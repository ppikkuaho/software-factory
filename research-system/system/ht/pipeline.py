"""The enforcement pipeline — the single ordered gate every mutation passes.

Order (A2 §4; the executor brief):
  load -> apply in memory (done by the command) ->
  schema-validate every touched doc -> role x field authority ->
  semantic rules -> write files -> regenerate index.json + index.live.json
  wholesale -> git commit the touched state files (HT_COMMIT=1, HT-Role trailer).

Commands build a Plan: the in-memory result plus a deferred `semantic` callable
for the pure checks (tier, anchors, epoch/instrument, merge-block, transitions,
freeze, cursor capacity) that run AFTER authority. Preconditions needed to compute
the mutation are checked inside the command during "apply in memory".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import authority, composed_tree, gitutil, indexing, jsonio, ledger_index, schemas
from .errors import HtError
from .paths import Root
from .repository_atomic import ExpectedPreimage, RepositoryTransaction


_LEDGER_SOURCE = re.compile(
    r"^ledger/(?P<book>[^/]+)/(?P<section>user|research|observatory)/L-[1-9][0-9]*\.json$"
)
_TREE_SOURCE = re.compile(r"^trees/(?P<component>[^/]+)/tree\.json$")
_NODE_SOURCE = re.compile(
    r"^trees/(?P<component>[^/]+)/nodes/(?P<node>[^/]+)/node\.json$"
)
_DISPATCH_SOURCE = re.compile(
    r"^trees/(?P<component>[^/]+)/nodes/(?P<node>[^/]+)/dispatches/[^/]+\.json$"
)
_ISSUE_SOURCE = re.compile(r"^tier1/issues/I-[1-9][0-9]*\.json$")


@dataclass
class DocWrite:
    path: Path
    doc_type: str
    old: dict | None
    new: dict
    ledger_section: str | None = None
    ledger_book: str | None = None
    # Fields in the harness authorship class that this specific Plan derives as
    # a mechanical consequence of the acting role's command.  Tier-1 state is
    # deliberately fail-closed: owning a harness field is not, by itself,
    # enough to hand-mutate it outside the command that derives it.
    mechanical_fields: frozenset[str] = frozenset()


@dataclass
class RawFile:
    """A report or archive copy (non-JSON). Archives are git-ignored."""

    dest: Path
    content: bytes
    gitignored: bool


@dataclass
class Plan:
    role: str
    message: str
    writes: list[DocWrite] = field(default_factory=list)
    raw_files: list[RawFile] = field(default_factory=list)
    regen_component: str | None = None
    semantic: Callable[[], None] | None = None
    warnings: list[str] = field(default_factory=list)


def _phase_mode(root: Root, plan: Plan) -> str:
    """Resolve the phase seen by this atomic Plan; absence fails closed."""
    for write in plan.writes:
        if write.doc_type == "phase":
            return write.new["mode"]
    if not root.phase_json.exists():
        return "sign-off"
    try:
        mode = jsonio.load(root.phase_json).get("mode")
    except Exception as exc:  # malformed state must fail closed, not become autonomy
        raise HtError(f"invalid tier1/phase.json: {exc} (A1 §10)") from exc
    if mode not in ("sign-off", "autonomy"):
        raise HtError(
            f"invalid tier1/phase.json mode '{mode}' (sign-off|autonomy) (A1 §10)"
        )
    return mode


def enforce_and_commit(
    root: Root,
    plan: Plan,
    *,
    lane=authority.UNASSIGNED_LANE,
) -> None:
    # gate 1 — schema conformance on every touched doc
    for w in plan.writes:
        schemas.validate(root.schemas_dir, w.doc_type, w.new)

    # gate 2 — role x field authority (A1 §10 / D10)
    phase = _phase_mode(root, plan)
    for w in plan.writes:
        authority.check_write(
            w.doc_type,
            w.old,
            w.new,
            plan.role,
            ledger_section=w.ledger_section,
            ledger_book=w.ledger_book,
            phase=phase,
            lane=lane,
            mechanical_fields=w.mechanical_fields,
        )

    # gate 3 — deferred semantic rules
    if plan.semantic is not None:
        plan.semantic()

    # --- all gates passed: freeze one HEAD+candidate repository transaction ---
    touched: list[str] = []
    expected: dict[Path, ExpectedPreimage] = {}
    output_paths: set[Path] = set()

    def expect(path: Path, preimage: ExpectedPreimage) -> None:
        if path in expected:
            raise HtError(
                f"repository transaction path appears twice in one Plan: {root.rel(path)}"
            )
        expected[path] = preimage

    for rf in plan.raw_files:
        # Reports, archives, adjudications, and observatory cards are all
        # write-once destinations.  Absence is an exact precondition, not just
        # a plan-construction observation.
        expect(rf.dest, None)
        output_paths.add(rf.dest)

    for w in plan.writes:
        old_bytes = None if w.old is None else jsonio.dumps(w.old).encode("utf-8")
        expect(w.path, old_bytes)
        output_paths.add(w.path)

    rebuild_union = any(w.doc_type == "ledger_entry" for w in plan.writes)
    rebuild_composed = any(
        w.doc_type in {"tree", "node", "dispatch", "issue"} for w in plan.writes
    )
    projection_outputs: list[Path] = []
    if rebuild_union:
        projection_outputs.append(root.ledger_union_index)
    if rebuild_composed:
        projection_outputs.append(root.composed_tree_json)
    if plan.regen_component is not None:
        projection_outputs.extend(
            (
                root.index_json(plan.regen_component),
                root.index_live_json(plan.regen_component),
            )
        )

    prefixes: set[str] = set()
    if rebuild_union:
        prefixes.add("ledger")
    if rebuild_composed:
        prefixes.update(("trees", "tier1/issues"))
    if plan.regen_component is not None:
        prefixes.add(f"trees/{plan.regen_component}")
    if projection_outputs:
        prefixes.add("readout")
    frozen_head = gitutil.head_snapshot(root.path, sorted(prefixes))

    def is_source(relative: str) -> bool:
        return bool(
            (rebuild_union and _LEDGER_SOURCE.fullmatch(relative))
            or (
                rebuild_composed
                and (
                    _TREE_SOURCE.fullmatch(relative)
                    or _NODE_SOURCE.fullmatch(relative)
                    or _DISPATCH_SOURCE.fullmatch(relative)
                    or _ISSUE_SOURCE.fullmatch(relative)
                )
            )
            or (
                plan.regen_component is not None
                and (
                    relative == f"trees/{plan.regen_component}/tree.json"
                    or re.fullmatch(
                        rf"trees/{re.escape(plan.regen_component)}/nodes/[^/]+/node\.json",
                        relative,
                    )
                    is not None
                )
            )
        )

    source_rels = {
        relative
        for relative, (kind, _content) in frozen_head.files.items()
        if is_source(relative)
    }
    for relative in source_rels:
        kind, content = frozen_head.files[relative]
        if kind != "regular":
            raise HtError(f"projection source is not a regular Git blob: {relative}")
        path = root.path / relative
        if path in expected:
            if expected[path] != content:
                raise HtError(
                    f"repository Plan source differs from frozen HEAD: {relative}"
                )
        else:
            expect(path, content)

    # Candidate source documents absent from HEAD join the frozen view; no
    # untracked worktree file can enter merely by matching a builder glob.
    for write in plan.writes:
        relative = root.rel(write.path)
        if is_source(relative):
            source_rels.add(relative)
            if relative not in frozen_head.files and expected[write.path] is not None:
                raise HtError(
                    f"repository source is absent from HEAD but Plan expects bytes: {relative}"
                )

    for path in projection_outputs:
        relative = root.rel(path)
        head_row = frozen_head.files.get(relative)
        preimage: ExpectedPreimage = None
        if head_row is not None:
            kind, content = head_row
            if kind != "regular":
                raise HtError(f"generated projection is not a regular Git blob: {relative}")
            preimage = content
        expect(path, preimage)
        output_paths.add(path)

    read_only = [
        root.path / relative
        for relative in source_rels
        if root.path / relative not in output_paths
    ]

    def frozen_json(transaction: RepositoryTransaction, relative: str) -> dict:
        try:
            value = json.loads(transaction.read_bytes(root.path / relative))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HtError(f"frozen repository source is invalid JSON: {relative}") from exc
        if not isinstance(value, dict):
            raise HtError(f"frozen repository source is not an object: {relative}")
        return value

    with RepositoryTransaction(
        root.path,
        expected,
        read_only=read_only,
        base_head=frozen_head.oid,
    ) as transaction:
        for rf in plan.raw_files:
            transaction.write_bytes(rf.dest, rf.content, gitignored=rf.gitignored)
            if not rf.gitignored:
                touched.append(root.rel(rf.dest))

        for w in plan.writes:
            transaction.write_bytes(w.path, jsonio.dumps(w.new).encode("utf-8"))
            touched.append(root.rel(w.path))

        # The union index is a generated, uncitable projection.  Regenerate it
        # from the complete in-transaction candidate so the same commit always
        # contains the resulting view; every ledger mutation takes this path.
        if rebuild_union:
            ledger_documents = []
            for relative in sorted(source_rels):
                match = _LEDGER_SOURCE.fullmatch(relative)
                if match is not None:
                    ledger_documents.append(
                        (
                            match.group("book"),
                            match.group("section"),
                            relative,
                            frozen_json(transaction, relative),
                        )
                    )
            union_doc = ledger_index.build_from_documents(ledger_documents)
            schemas.validate(root.schemas_dir, "ledger_union_index", union_doc)
            transaction.write_bytes(
                root.ledger_union_index,
                jsonio.dumps(union_doc).encode("utf-8"),
            )
            touched.append(root.rel(root.ledger_union_index))

        # F10 follows the same wholesale regeneration discipline.  Its pure
        # builder sees every candidate node/dispatch/issue write above.
        if rebuild_composed:
            trees: list[dict] = []
            tree_rels = sorted(
                relative
                for relative in source_rels
                if _TREE_SOURCE.fullmatch(relative)
            )
            for tree_rel in tree_rels:
                tree_match = _TREE_SOURCE.fullmatch(tree_rel)
                assert tree_match is not None
                component = tree_match.group("component")
                nodes: list[dict] = []
                node_rels = sorted(
                    relative
                    for relative in source_rels
                    if (
                        (match := _NODE_SOURCE.fullmatch(relative)) is not None
                        and match.group("component") == component
                    )
                )
                for node_rel in node_rels:
                    node_match = _NODE_SOURCE.fullmatch(node_rel)
                    assert node_match is not None
                    node_id = node_match.group("node")
                    dispatches = [
                        frozen_json(transaction, relative)
                        for relative in sorted(source_rels)
                        if (
                            (match := _DISPATCH_SOURCE.fullmatch(relative)) is not None
                            and match.group("component") == component
                            and match.group("node") == node_id
                        )
                    ]
                    nodes.append(
                        {"node": frozen_json(transaction, node_rel), "dispatches": dispatches}
                    )
                trees.append(
                    {
                        "component": component,
                        "tree": frozen_json(transaction, tree_rel),
                        "nodes": nodes,
                    }
                )
            issues = [
                frozen_json(transaction, relative)
                for relative in sorted(source_rels)
                if _ISSUE_SOURCE.fullmatch(relative)
            ]
            composed_doc = composed_tree.build_from_documents(trees, issues)
            schemas.validate(root.schemas_dir, "composed_tree", composed_doc)
            transaction.write_bytes(
                root.composed_tree_json,
                jsonio.dumps(composed_doc).encode("utf-8"),
            )
            touched.append(root.rel(root.composed_tree_json))

        # Wholesale component-index regeneration (J7), likewise built and
        # validated inside the candidate before the Git publication point.
        if plan.regen_component is not None:
            component = plan.regen_component
            tree_rel = f"trees/{component}/tree.json"
            nodes = [
                frozen_json(transaction, relative)
                for relative in sorted(source_rels)
                if (
                    (match := _NODE_SOURCE.fullmatch(relative)) is not None
                    and match.group("component") == component
                )
            ]
            full_index, live_index = indexing.build_from_documents(
                frozen_json(transaction, tree_rel), nodes
            )
            for path, document in (
                (root.index_json(plan.regen_component), full_index),
                (root.index_live_json(plan.regen_component), live_index),
            ):
                schemas.validate(root.schemas_dir, "index", document)
                transaction.write_bytes(path, jsonio.dumps(document).encode("utf-8"))
                touched.append(root.rel(path))

        transaction.commit(touched, plan.role, plan.message)
