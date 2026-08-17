"""`ht validate [--all | --tree C]` — schema-validate state files (read-only).

Also the logic the hook reuses for staged content. Returns a list of human-readable
error strings (empty == all valid).
"""

from __future__ import annotations

import re

from .. import classify, composed_tree, jsonio, ledger_index, schemas
from ..errors import HtError, HtUsageError
from ..references import resolve_for_issue, resolve_ref
from ._common import Ctx, all_components


_LEDGER_SECTIONS = ("user", "research", "observatory")
_SUBGOAL_REF = re.compile(
    r"^(?:tree#(?P<tree>[^/]+)/)?(?P<kind>dispatch|node)#(?P<target>[^/]+)$"
)


def _registered_ledger_books(ctx: Ctx) -> set[str]:
    """Return the book names authorized by the federated-ledger layout.

    ``top`` is the root book.  Every other book must be backed by a registered
    lane, represented physically by ``trees/<lane>/tree.json``.  Deriving this
    set from the tree records (rather than from populated ledger entries) makes
    an empty mistyped book directory visible to validation.
    """
    books = {"top"}
    trees_dir = ctx.root.trees_dir
    if trees_dir.is_dir():
        books.update(path.parent.name for path in trees_dir.glob("*/tree.json"))
    return books


def _ledger_book_dirs(ctx: Ctx):
    """Yield every immediate ledger book directory deterministically."""
    ledger_dir = ctx.root.ledger_dir
    if ledger_dir.is_dir():
        yield from sorted(path for path in ledger_dir.iterdir() if path.is_dir())


def _ledger_layout_errors(ctx: Ctx) -> list[str]:
    """Report unknown books, including empty orphan directories, loudly."""
    registered = _registered_ledger_books(ctx)
    return [
        f"ledger/{book_dir.name}: orphan ledger book '{book_dir.name}' "
        "(expected 'top' or a registered lane with trees/<lane>/tree.json)"
        for book_dir in _ledger_book_dirs(ctx)
        if book_dir.name not in registered
    ]


def _iter_state_files(ctx: Ctx, all_flag: bool, tree_opt: str | None):
    root = ctx.root
    if tree_opt is not None:
        components = [tree_opt]
        if not root.tree_json(tree_opt).exists():
            raise HtUsageError(f"no such tree '{tree_opt}'")
    else:
        from ._common import all_components
        components = all_components(ctx)

    for c in components:
        for p in (root.tree_json(c), root.index_json(c), root.index_live_json(c)):
            if p.exists():
                yield p
        nodes_dir = root.nodes_dir(c)
        if nodes_dir.is_dir():
            for nj in sorted(nodes_dir.glob("*/node.json")):
                yield nj
            for dj in sorted(nodes_dir.glob("*/dispatches/*.json")):
                yield dj

    if all_flag or tree_opt is None:
        for book_dir in _ledger_book_dirs(ctx):
            for section in _LEDGER_SECTIONS:
                section_dir = book_dir / section
                if section_dir.is_dir():
                    yield from sorted(section_dir.glob("L-*.json"))
        if root.tier1_dir.is_dir():
            for tier1_json in sorted(root.tier1_dir.rglob("*.json")):
                yield tier1_json


def _union_index_errors(ctx: Ctx) -> list[str]:
    """Report a missing, unreadable, schema-invalid, or stale union view."""
    path = ctx.root.ledger_union_index
    rel = ctx.root.rel(path)
    if not path.exists():
        return [f"{rel}: missing generated ledger union index"]
    try:
        actual = jsonio.load(path)
    except Exception as exc:  # noqa: BLE001
        return [f"{rel}: unreadable JSON ({exc})"]
    try:
        schemas.validate(ctx.root.schemas_dir, "ledger_union_index", actual)
    except HtError as exc:
        return [f"{rel}: {exc.message}"]
    try:
        expected = ledger_index.build(ctx.root)
    except Exception as exc:  # noqa: BLE001
        return [f"{rel}: cannot build generated ledger union index ({exc})"]
    if actual != expected:
        return [
            f"{rel}: stale or inconsistent generated ledger union index "
            "(regenerate from canonical ledger entries)"
        ]
    return []


def _composed_tree_errors(ctx: Ctx) -> list[str]:
    """Report missing, invalid, or stale F10 projection state."""
    path = ctx.root.composed_tree_json
    rel = ctx.root.rel(path)
    if not path.exists():
        return [f"{rel}: missing generated composed tree"]
    try:
        actual = jsonio.load(path)
        schemas.validate(ctx.root.schemas_dir, "composed_tree", actual)
        expected = composed_tree.build(ctx.root)
    except Exception as exc:  # noqa: BLE001
        detail = exc.message if isinstance(exc, HtError) else str(exc)
        return [f"{rel}: cannot validate generated composed tree ({detail})"]
    if actual != expected:
        return [
            f"{rel}: stale or inconsistent generated composed tree "
            "(regenerate from canonical lane trees and issues)"
        ]
    return []


def _load_join_state(ctx: Ctx, tree_opt: str | None):
    """Load the issue/node/dispatch join surface without changing validation state.

    Unreadable or schema-invalid documents remain the ordinary validator's concern;
    this warning-only hygiene pass skips them so it can never turn drift into an
    error or obscure the primary validation diagnosis.
    """
    # Join targets are global.  Loading only ``tree_opt`` would make a valid
    # tree-qualified ref into another tree look absent during ``validate --tree``.
    del tree_opt
    components = all_components(ctx)
    issues: dict[str, dict] = {}
    issue_dir = ctx.root.tier1_dir / "issues"
    if issue_dir.is_dir():
        for path in sorted(issue_dir.glob("I-*.json")):
            try:
                issue = jsonio.load(path)
            except Exception:  # noqa: BLE001 - ordinary validation reports it
                continue
            if isinstance(issue, dict):
                issues[path.stem] = issue

    nodes: dict[tuple[str, str], dict] = {}
    dispatches: dict[tuple[str, str], dict] = {}
    for component in components:
        nodes_dir = ctx.root.nodes_dir(component)
        if not nodes_dir.is_dir():
            continue
        for path in sorted(nodes_dir.glob("*/node.json")):
            try:
                node = jsonio.load(path)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(node, dict):
                continue
            nodes[(component, str(node.get("id", path.parent.name)))] = node
        for path in sorted(nodes_dir.glob("*/dispatches/*.json")):
            try:
                dispatch = jsonio.load(path)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(dispatch, dict):
                continue
            dispatches[(component, str(dispatch.get("id", path.stem)))] = dispatch
    return issues, nodes, dispatches


def _subgoal_matches(
    value: object,
    *,
    component: str,
    kind: str,
    target: str,
) -> bool:
    """Accept the established unqualified form and a tree-qualified form."""
    if value == f"{kind}#{target}":
        return True
    return value == f"tree#{component}/{kind}#{target}"


def _resolve_subgoal_target(
    mapping: dict[tuple[str, str], dict],
    *,
    tree_name: str | None,
    target: str,
) -> list[tuple[tuple[str, str], dict]]:
    if tree_name is not None:
        key = (tree_name, target)
        return [(key, mapping[key])] if key in mapping else []
    return [(key, doc) for key, doc in mapping.items() if key[1] == target]


def join_asymmetry_warnings(
    ctx: Ctx,
    all_flag: bool,
    tree_opt: str | None,
) -> list[str]:
    """Return warning-only issue join drift for the CLI to print on stderr.

    Authority remains one-way at each write: dispatch/node commands write only
    their own back-reference, and ``ht issue subgoal-add`` owns the issue side.
    This read-only pass merely makes missing reciprocal bookkeeping loud
    (coherence amendments §4; rider b).
    """
    del all_flag  # joins are global whenever validation includes Tier-1 state
    issues, nodes, dispatches = _load_join_state(ctx, tree_opt)
    warnings: list[str] = []

    for (component, dispatch_id), dispatch in sorted(dispatches.items()):
        issue_id = dispatch.get("issue_ref")
        if not isinstance(issue_id, str):
            continue
        if issue_id not in issues:
            warnings.append(
                f"join asymmetry: dispatch {dispatch_id} in tree {component} cites "
                f"missing issue {issue_id} (coherence amendments §4)"
            )
            continue
        subgoals = issues[issue_id].get("subgoals", [])
        if not isinstance(subgoals, list):
            continue  # ordinary schema validation owns malformed field types
        if not any(
            _subgoal_matches(
                value,
                component=component,
                kind="dispatch",
                target=dispatch_id,
            )
            for value in subgoals
        ):
            warnings.append(
                f"join asymmetry: dispatch {dispatch_id} in tree {component} cites "
                f"issue {issue_id}, but that issue does not list "
                f"dispatch#{dispatch_id} in subgoals (coherence amendments §4)"
            )

    for issue_id, issue in sorted(issues.items()):
        subgoals = issue.get("subgoals", [])
        if not isinstance(subgoals, list):
            continue  # ordinary schema validation owns malformed field types
        for value in subgoals:
            if not isinstance(value, str):
                continue
            match = _SUBGOAL_REF.fullmatch(value)
            if match is None:
                continue
            kind = match.group("kind")
            target = match.group("target")
            tree_name = match.group("tree")
            mapping = dispatches if kind == "dispatch" else nodes
            resolved = _resolve_subgoal_target(
                mapping,
                tree_name=tree_name,
                target=target,
            )
            if len(resolved) != 1:
                reason = (
                    "not found"
                    if not resolved
                    else "ambiguous without tree qualification"
                )
                warnings.append(
                    f"join asymmetry: issue {issue_id} subgoal {value} is {reason} "
                    f"(coherence amendments §4)"
                )
                continue
            (_component, _target), document = resolved[0]
            back_ref = (
                document.get("issue_ref")
                if kind == "dispatch"
                else document.get("minted_from")
            )
            expected = issue_id if kind == "dispatch" else f"issue#{issue_id}"
            if back_ref != expected:
                warnings.append(
                    f"join asymmetry: issue {issue_id} lists {value}, but its {kind} "
                    f"back-reference is {back_ref!r}, expected {expected!r} "
                    f"(coherence amendments §4)"
                )
    return warnings


def lca_rehoming_debt_inventory(
    ctx: Ctx,
    all_flag: bool,
    tree_opt: str | None,
) -> list[str]:
    """Return informational rows for ledger entries awaiting LCA re-homing.

    Debt is a global ledger concern, so it is visible for the default and
    ``--all`` validation scopes.  A tree-specific validation retains its
    existing file-selection boundary.  Malformed documents are left to the
    ordinary error channel rather than making this informational pass fail.
    """
    if tree_opt is not None and not all_flag:
        return []

    inventory: list[str] = []
    for book_dir in _ledger_book_dirs(ctx):
        for section in _LEDGER_SECTIONS:
            section_dir = book_dir / section
            if not section_dir.is_dir():
                continue
            for path in sorted(section_dir.glob("L-*.json")):
                try:
                    entry = jsonio.load(path)
                except Exception:  # noqa: BLE001 - run() owns the validation error
                    continue
                if not isinstance(entry, dict):
                    continue
                intended_scope = entry.get("intended_scope")
                if not isinstance(intended_scope, str) or not intended_scope.strip():
                    continue
                entry_id = entry.get("id", path.stem)
                inventory.append(
                    f"LCA re-homing debt: {entry_id} (book {book_dir.name}) "
                    f"intended_scope={intended_scope}"
                )
    return inventory


def run(ctx: Ctx, all_flag: bool, tree_opt: str | None) -> list[str]:
    errors: list[str] = []
    if all_flag or tree_opt is None:
        errors.extend(_ledger_layout_errors(ctx))
        try:
            errors.extend(ledger_index.invariant_errors(ctx.root))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ledger: cannot validate global identity invariant ({exc})")
    for path in _iter_state_files(ctx, all_flag, tree_opt):
        rel = ctx.root.rel(path)
        dt = classify.doc_type(rel)
        if dt is None:
            continue
        try:
            doc = jsonio.load(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{rel}: unreadable JSON ({exc})")
            continue
        try:
            schemas.validate(ctx.root.schemas_dir, dt, doc)
        except HtError as exc:
            errors.append(f"{rel}: {exc.message}")
    if all_flag or tree_opt is None:
        errors.extend(_union_index_errors(ctx))
        errors.extend(_composed_tree_errors(ctx))
        errors.extend(_reference_errors(ctx))
    return errors


def _reference_errors(ctx: Ctx) -> list[str]:
    """Validate only Wave-A shapes; legacy strings remain compatibility data."""
    errors: list[str] = []
    for dispatch_path in sorted(
        ctx.root.trees_dir.glob("*/nodes/*/dispatches/*.json")
    ):
        try:
            dispatch = jsonio.load(dispatch_path)
            tree_name = dispatch_path.relative_to(ctx.root.trees_dir).parts[0]
            expected_dispatch = f"tree#{tree_name}/dispatch#{dispatch['id']}"
            expected_node = f"tree#{tree_name}/node#{dispatch['node']}"
            for value in dispatch.get("adjudications", []):
                if not isinstance(value, str) or not value.startswith("tree#"):
                    continue
                adjudication = resolve_ref(
                    ctx.root, value, expected={"adjudication"}
                )
                if adjudication.document.get("dispatch_ref") != expected_dispatch:
                    raise HtError(
                        f"{value} does not match containing dispatch {expected_dispatch}"
                    )
                if adjudication.document.get("node_ref") != expected_node:
                    raise HtError(f"{value} does not match containing node {expected_node}")
        except (HtError, HtUsageError, KeyError, TypeError) as exc:
            errors.append(f"{ctx.root.rel(dispatch_path)}: {exc}")

    issue_dir = ctx.root.tier1_dir / "issues"
    if issue_dir.is_dir():
        from . import issue as issue_commands

        for issue_path in sorted(issue_dir.glob("I-*.json")):
            try:
                issue = jsonio.load(issue_path)
                issue_id = issue["id"]
                for attachment in issue.get("observatory_attachments", []):
                    resolved = resolve_ref(
                        ctx.root,
                        attachment["ref"],
                        expected={"observatory-report"},
                    )
                    if (resolved.metadata or {}).get("sha256") != attachment["sha256"]:
                        raise HtError(
                            f"observatory attachment {attachment['ref']} hash mismatch"
                        )
                closure = issue.get("closure")
                if isinstance(closure, dict) and "dispositions" in closure:
                    canonical = issue_commands._closure_refs(
                        ctx, issue, list(closure.get("refs", []))
                    )
                    if canonical != closure.get("refs"):
                        raise HtError("new-shape closure refs are not canonical")
                    for row in closure.get("dispositions", []):
                        resolved = resolve_for_issue(
                            ctx.root,
                            row["ref"],
                            issue_id,
                            expected={"dispatch"},
                        )
                        if resolved.canonical != row["ref"]:
                            raise HtError(
                                f"closure disposition {row['ref']} is not canonical"
                            )
            except (HtError, HtUsageError, KeyError, TypeError) as exc:
                errors.append(f"{ctx.root.rel(issue_path)}: {exc}")

    record_dir = ctx.root.tier1_dir / "merge-records"
    if record_dir.is_dir():
        from . import mrec

        for record_path in sorted(record_dir.glob("MR-*.json")):
            try:
                record = jsonio.load(record_path)
                if (
                    "lane_adjudication_ref" not in record
                    and "backing_claims" not in record
                ):
                    continue
                if (
                    "lane_adjudication_ref" not in record
                    or "backing_claims" not in record
                ):
                    raise HtError("merge record has only a partial Wave-A anchor")
                backing = mrec.verify_backing_snapshot(ctx.root, record)
                candidate = resolve_ref(
                    ctx.root, record["candidate_ref"], expected={"node"}
                )
                lane = resolve_ref(
                    ctx.root,
                    record["lane_adjudication_ref"],
                    expected={"adjudication"},
                )
                if lane.document.get("node_ref") != candidate.canonical:
                    raise HtError("lane adjudication does not belong to candidate")
                if lane.document.get("claim_ref") not in {
                    item["ref"] for item in backing
                }:
                    raise HtError("lane adjudication does not name a backing claim")
                affiliations = set(candidate.issue_refs) | set(lane.issue_refs)
                for item in backing:
                    affiliations.update(
                        resolve_ref(
                            ctx.root, item["ref"], expected={"claim"}
                        ).issue_refs
                    )
                if len(affiliations) > 1:
                    raise HtError(
                        "merge record has conflicting issue affiliations: "
                        + ", ".join(sorted(affiliations))
                    )
            except (HtError, HtUsageError, KeyError, TypeError) as exc:
                errors.append(f"{ctx.root.rel(record_path)}: {exc}")
    return errors
