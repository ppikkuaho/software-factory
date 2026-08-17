"""Strict typed-reference parsing and deterministic research-state resolution.

This module is the only grammar owner for Wave-A references.  Command modules
ask it to resolve values before constructing a mutation Plan; successful writes
therefore persist canonical, fully qualified identities rather than CLI shorthands.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from . import jsonio
from .errors import HtError, HtUsageError
from .paths import Root, normalize_repository_relpath


ADJUDICATION_HEADER_PREFIX = "HT-ADJUDICATION "

_ISSUE = re.compile(r"issue#(?P<id>I-[0-9]+)", re.ASCII)
_LEDGER = re.compile(r"(?P<alias>ledger(?:-entry)?)#(?P<id>L-[0-9]+)", re.ASCII)
_MREC = re.compile(r"merge-record#(?P<id>MR-[0-9]+)", re.ASCII)
_OBSERVATORY = re.compile(
    r"observatory-report#(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)", re.ASCII
)
_B2_REPOSITORY = re.compile(
    r"(?P<kind>subgoal|task-package|inbox-delivery|inbox-receipt|action-receipt)#"
    r"(?P<id>(?:SG|TP|IB|AR)-[1-9][0-9]*)",
    re.ASCII,
)
_PC = re.compile(r"pc#(?P<id>principal-coordinator)", re.ASCII)
_ISSUE_QUEUE = re.compile(r"issue-queue#(?P<id>current)", re.ASCII)
_RATIFICATION_ITEM = re.compile(r"ratification-item#(?P<id>RQ-[1-9][0-9]*)", re.ASCII)
_REPOSITORY_FILE = re.compile(r"repository-file#(?P<id>.+)", re.ASCII)
_QUALIFIED = re.compile(
    r"tree#(?P<tree>[^/#\s]+)/"
    r"(?P<kind>node|dispatch|plan|report|unit-artifact|qa|archive|qa-set|claim|adjudication)#(?P<id>[^/#\s]+)",
    re.ASCII,
)
_LOCAL = re.compile(
    r"(?P<kind>node|dispatch|report|claim|adjudication)#(?P<id>[^/#\s]+)",
    re.ASCII,
)
_LEGACY_ADJUDICATION_PATH = re.compile(
    r"trees/(?P<tree>[^/]+)/nodes/(?P<node>[1-9][0-9]*(?:\.[1-9][0-9]*)*)/"
    r"adjudications/(?P<id>d-[1-9][0-9]*(?:\.[1-9][0-9]*)*-[1-9][0-9]*-a[1-9][0-9]*)\.md",
    re.ASCII,
)
_NODE_ID = re.compile(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", re.ASCII)
_DISPATCH_ID = re.compile(
    r"d-(?P<node>[1-9][0-9]*(?:\.[1-9][0-9]*)*)-[1-9][0-9]*", re.ASCII
)
_CLAIM_ID = re.compile(
    r"c-(?P<node>[1-9][0-9]*(?:\.[1-9][0-9]*)*)-[1-9][0-9]*", re.ASCII
)
_ADJUDICATION_ID = re.compile(
    r"(?P<dispatch>d-[1-9][0-9]*(?:\.[1-9][0-9]*)*-[1-9][0-9]*)-a[1-9][0-9]*",
    re.ASCII,
)
_UNIT_ARTIFACT_ID = re.compile(
    r"(?P<dispatch>d-[1-9][0-9]*(?:\.[1-9][0-9]*)*-[1-9][0-9]*)@"
    r"TP-[1-9][0-9]*@[a-z][a-z0-9-]{0,63}",
    re.ASCII,
)
_QA_ID = re.compile(
    r"(?P<dispatch>d-[1-9][0-9]*(?:\.[1-9][0-9]*)*-[1-9][0-9]*)@"
    r"TP-[1-9][0-9]*@qa",
    re.ASCII,
)

TREE_KINDS = frozenset(
    {
        "node", "dispatch", "plan", "report", "unit-artifact", "qa",
        "archive", "qa-set", "claim", "adjudication",
    }
)
ALL_KINDS = TREE_KINDS | frozenset(
    {
        "issue", "ledger", "merge-record", "observatory-report", "subgoal",
        "task-package", "inbox-delivery", "inbox-receipt", "action-receipt",
        "pc", "issue-queue", "ratification-item", "repository-file",
    }
)

_B2_PARSE_ONLY_KINDS = frozenset(
    {
        "subgoal", "task-package", "inbox-delivery", "inbox-receipt",
        "action-receipt", "issue-queue", "ratification-item", "repository-file",
        "plan", "unit-artifact", "qa", "archive", "qa-set",
    }
)


@dataclass(frozen=True)
class ParsedRef:
    kind: str
    object_id: str
    tree: str | None
    canonical: str
    local: bool = False
    compatibility_alias: bool = False


@dataclass(frozen=True)
class ResolvedRef:
    kind: str
    canonical: str
    path: Path | None
    tree: str | None
    object_id: str
    issue_refs: tuple[str, ...]
    document: Any = None
    metadata: dict[str, Any] | None = None

    @property
    def issue_ref(self) -> str | None:
        return self.issue_refs[0] if len(self.issue_refs) == 1 else None


def canonical_issue_ref(issue_id: str | None) -> str | None:
    if issue_id is None:
        return None
    if not isinstance(issue_id, str) or re.fullmatch(r"I-[0-9]+", issue_id) is None:
        raise HtError(f"invalid issue identity {issue_id!r}")
    return f"issue#{issue_id}"


def canonical_json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HtError(f"value cannot be canonicalized as JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def parse_ref(value: str) -> ParsedRef:
    if not isinstance(value, str) or not value:
        raise HtUsageError("typed reference must be a non-empty string")
    if value != value.strip():
        raise HtUsageError(f"malformed typed reference {value!r}: whitespace is not allowed")

    match = _ISSUE.fullmatch(value)
    if match is not None:
        object_id = match.group("id")
        return ParsedRef("issue", object_id, None, f"issue#{object_id}")
    match = _LEDGER.fullmatch(value)
    if match is not None:
        object_id = match.group("id")
        return ParsedRef(
            "ledger",
            object_id,
            None,
            f"ledger#{object_id}",
            compatibility_alias=match.group("alias") == "ledger-entry",
        )
    match = _MREC.fullmatch(value)
    if match is not None:
        object_id = match.group("id")
        return ParsedRef("merge-record", object_id, None, f"merge-record#{object_id}")
    match = _OBSERVATORY.fullmatch(value)
    if match is not None:
        object_id = match.group("id")
        if object_id in {".", ".."}:
            raise HtUsageError(f"malformed observatory report ref {value!r}")
        return ParsedRef(
            "observatory-report",
            object_id,
            None,
            f"observatory-report#{object_id}",
        )
    match = _B2_REPOSITORY.fullmatch(value)
    if match is not None:
        kind = match.group("kind")
        object_id = match.group("id")
        expected_prefix = {
            "subgoal": "SG-",
            "task-package": "TP-",
            "inbox-delivery": "IB-",
            "inbox-receipt": "IB-",
            "action-receipt": "AR-",
        }[kind]
        if not object_id.startswith(expected_prefix):
            raise HtUsageError(f"malformed {kind} reference id {object_id!r}")
        return ParsedRef(kind, object_id, None, f"{kind}#{object_id}")
    match = _PC.fullmatch(value)
    if match is not None:
        return ParsedRef("pc", match.group("id"), None, "pc#principal-coordinator")
    match = _ISSUE_QUEUE.fullmatch(value)
    if match is not None:
        return ParsedRef("issue-queue", match.group("id"), None, "issue-queue#current")
    match = _RATIFICATION_ITEM.fullmatch(value)
    if match is not None:
        object_id = match.group("id")
        return ParsedRef("ratification-item", object_id, None, f"ratification-item#{object_id}")
    match = _REPOSITORY_FILE.fullmatch(value)
    if match is not None:
        object_id = match.group("id")
        _validate_repository_relpath(object_id)
        return ParsedRef("repository-file", object_id, None, f"repository-file#{object_id}")
    match = _QUALIFIED.fullmatch(value)
    if match is not None:
        kind = match.group("kind")
        tree = match.group("tree")
        object_id = match.group("id")
        _validate_tree_object_id(kind, object_id)
        return ParsedRef(kind, object_id, tree, f"tree#{tree}/{kind}#{object_id}")
    match = _LEGACY_ADJUDICATION_PATH.fullmatch(value)
    if match is not None:
        tree = match.group("tree")
        object_id = match.group("id")
        adj_match = _ADJUDICATION_ID.fullmatch(object_id)
        assert adj_match is not None
        dispatch_match = _DISPATCH_ID.fullmatch(adj_match.group("dispatch"))
        assert dispatch_match is not None
        if dispatch_match.group("node") != match.group("node"):
            raise HtUsageError(
                f"malformed legacy adjudication path {value!r}: node identity differs"
            )
        return ParsedRef(
            "adjudication",
            object_id,
            tree,
            f"tree#{tree}/adjudication#{object_id}",
            compatibility_alias=True,
        )
    match = _LOCAL.fullmatch(value)
    if match is not None:
        kind = match.group("kind")
        object_id = match.group("id")
        _validate_tree_object_id(kind, object_id)
        return ParsedRef(kind, object_id, None, value, local=True)
    raise HtUsageError(f"malformed typed reference {value!r}")


def _validate_tree_object_id(kind: str, object_id: str) -> None:
    patterns = {
        "node": _NODE_ID,
        "dispatch": _DISPATCH_ID,
        "plan": _DISPATCH_ID,
        "report": _DISPATCH_ID,
        "unit-artifact": _UNIT_ARTIFACT_ID,
        "qa": _QA_ID,
        "archive": _DISPATCH_ID,
        "qa-set": _DISPATCH_ID,
        "claim": _CLAIM_ID,
        "adjudication": _ADJUDICATION_ID,
    }
    if patterns[kind].fullmatch(object_id) is None:
        raise HtUsageError(f"malformed {kind} reference id {object_id!r}")


def _validate_repository_relpath(value: str) -> None:
    try:
        normalize_repository_relpath(value)
    except HtUsageError as exc:
        raise HtUsageError(f"malformed repository-file path {value!r}: {exc}") from exc


def _check_expected(parsed: ParsedRef, expected: Iterable[str] | None) -> None:
    if expected is None:
        return
    allowed = frozenset(expected)
    if parsed.kind not in allowed:
        names = ", ".join(sorted(allowed))
        raise HtUsageError(
            f"typed reference {parsed.canonical!r} has type {parsed.kind}; "
            f"expected {names}"
        )


def resolve_ref(
    root: Root,
    value: str,
    *,
    expected: Iterable[str] | None = None,
) -> ResolvedRef:
    parsed = parse_ref(value)
    _check_expected(parsed, expected)
    if parsed.kind == "pc":
        return ResolvedRef(
            "pc", parsed.canonical, None, None, parsed.object_id, (), None,
            {"virtual": True},
        )
    if parsed.kind in _B2_PARSE_ONLY_KINDS:
        raise HtUsageError(
            f"typed reference {parsed.canonical!r} is parsed by the B2 foundation; "
            "its stable repository resolver is not installed yet"
        )
    if parsed.kind == "issue":
        path = root.issue_json(parsed.object_id)
        if not _regular_file_in(root, path, root.tier1_dir / "issues"):
            raise HtUsageError(f"no such issue '{parsed.object_id}'")
        document = jsonio.load(path)
        if document.get("id") != parsed.object_id:
            raise HtError(
                f"issue path {root.rel(path)!r} contains id {document.get('id')!r}, "
                f"requires {parsed.object_id!r}"
            )
        return ResolvedRef(
            parsed.kind,
            parsed.canonical,
            path,
            None,
            parsed.object_id,
            (parsed.canonical,),
            document,
        )
    if parsed.kind == "ledger":
        return _resolve_ledger(root, parsed)
    if parsed.kind == "merge-record":
        return _resolve_merge_record(root, parsed)
    if parsed.kind == "observatory-report":
        return _resolve_observatory(root, parsed)
    return _resolve_tree_ref(root, parsed)


def resolve_for_issue(
    root: Root,
    value: str,
    issue_id: str,
    *,
    expected: Iterable[str],
) -> ResolvedRef:
    resolved = resolve_ref(root, value, expected=expected)
    target = canonical_issue_ref(issue_id)
    assert target is not None
    if resolved.kind == "observatory-report":
        return resolved
    if resolved.issue_refs != (target,):
        actual = ", ".join(resolved.issue_refs) if resolved.issue_refs else "unaffiliated"
        raise HtError(
            f"reference {resolved.canonical} belongs to {actual}, not {target}"
        )
    return resolved


def _regular_file_in(root: Root, path: Path, canonical_parent: Path) -> bool:
    """Require a lexical canonical regular file with no symlinked component."""
    root_path = root.path.resolve()
    lexical = path.absolute()
    parent = canonical_parent.absolute()
    if not lexical.is_relative_to(parent) or not parent.is_relative_to(root_path):
        raise HtError(f"state path {lexical} escapes its canonical research-root lane")
    try:
        relative = lexical.relative_to(root_path)
    except ValueError as exc:  # pragma: no cover - guarded above
        raise HtError(f"state path {lexical} escapes the research root") from exc
    cursor = root_path
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HtError(f"state path {lexical} contains symlink component {cursor}")
    if not path.is_file():
        return False
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HtError(f"cannot resolve state path {lexical}: {exc}") from exc
    if resolved != lexical or not resolved.is_relative_to(root_path):
        raise HtError(f"state path {lexical} is not a canonical confined regular file")
    return True


def _tree_components(root: Root) -> list[str]:
    if not root.trees_dir.is_dir() or root.trees_dir.is_symlink():
        return []
    components: list[str] = []
    for directory in sorted(root.trees_dir.iterdir(), key=lambda path: path.name):
        if directory.is_symlink() or not directory.is_dir():
            continue
        tree_path = directory / "tree.json"
        if not _regular_file_in(root, tree_path, directory):
            continue
        document = jsonio.load(tree_path)
        if document.get("component") != directory.name:
            continue
        components.append(directory.name)
    return components


def _candidate_tree_paths(root: Root, parsed: ParsedRef) -> list[tuple[str, Path]]:
    components = _tree_components(root)
    if parsed.tree is not None:
        if parsed.tree in {".", ".."} or parsed.tree not in components:
            raise HtUsageError(f"no such canonical tree '{parsed.tree}'")
        trees = [parsed.tree]
    else:
        trees = components
    matches: list[tuple[str, Path]] = []
    for tree in trees:
        assert tree is not None
        if parsed.kind == "node":
            path = root.node_json(tree, parsed.object_id)
        elif parsed.kind in {"dispatch", "report"}:
            dispatch_match = _DISPATCH_ID.fullmatch(parsed.object_id)
            assert dispatch_match is not None
            path = root.dispatch_json(tree, dispatch_match.group("node"), parsed.object_id)
        elif parsed.kind == "claim":
            claim_match = _CLAIM_ID.fullmatch(parsed.object_id)
            assert claim_match is not None
            path = root.node_json(tree, claim_match.group("node"))
        else:
            adj_match = _ADJUDICATION_ID.fullmatch(parsed.object_id)
            assert adj_match is not None
            dispatch_match = _DISPATCH_ID.fullmatch(adj_match.group("dispatch"))
            assert dispatch_match is not None
            path = root.adjudication_path(tree, dispatch_match.group("node"), parsed.object_id)
        if _regular_file_in(root, path, root.tree_dir(tree)):
            if parsed.kind == "claim":
                node = jsonio.load(path)
                if node.get("id") != _CLAIM_ID.fullmatch(parsed.object_id).group("node"):
                    raise HtError(
                        f"claim {parsed.object_id!r} containing node identity does not "
                        "match its canonical path"
                    )
                if not any(
                    isinstance(claim, dict) and claim.get("id") == parsed.object_id
                    for claim in node.get("claims", [])
                ):
                    continue
            matches.append((tree, path))
    return matches


def _resolve_tree_ref(root: Root, parsed: ParsedRef) -> ResolvedRef:
    matches = _candidate_tree_paths(root, parsed)
    if not matches:
        where = f" in tree '{parsed.tree}'" if parsed.tree is not None else ""
        raise HtUsageError(f"no such {parsed.kind} '{parsed.object_id}'{where}")
    if len(matches) > 1:
        candidates = [
            f"tree#{tree}/{parsed.kind}#{parsed.object_id}" for tree, _path in matches
        ]
        raise HtUsageError(
            f"{parsed.kind} ref '{parsed.kind}#{parsed.object_id}' is ambiguous: "
            f"{', '.join(sorted(candidates))}; qualify with tree#.../"
        )
    tree, path = matches[0]
    canonical = f"tree#{tree}/{parsed.kind}#{parsed.object_id}"
    if parsed.kind == "node":
        node = jsonio.load(path)
        if node.get("id") != parsed.object_id:
            raise HtError(
                f"node {canonical} path/document identity mismatch: {node.get('id')!r}"
            )
        issue_refs = _node_issue_refs(node)
        return ResolvedRef("node", canonical, path, tree, parsed.object_id, issue_refs, node)
    if parsed.kind == "dispatch":
        dispatch = jsonio.load(path)
        _verify_dispatch_identity(tree, parsed.object_id, dispatch)
        return ResolvedRef(
            "dispatch",
            canonical,
            path,
            tree,
            parsed.object_id,
            _dispatch_issue_refs(dispatch),
            dispatch,
        )
    if parsed.kind == "report":
        dispatch = jsonio.load(path)
        dispatch_match = _DISPATCH_ID.fullmatch(parsed.object_id)
        assert dispatch_match is not None
        _verify_dispatch_identity(tree, parsed.object_id, dispatch)
        report_path, digest = _verified_report(
            root,
            tree,
            dispatch,
            expected_dispatch_id=parsed.object_id,
            expected_node_id=dispatch_match.group("node"),
        )
        return ResolvedRef(
            "report",
            canonical,
            report_path,
            tree,
            parsed.object_id,
            _dispatch_issue_refs(dispatch),
            None,
            {"sha256": digest, "dispatch": dispatch},
        )
    if parsed.kind == "claim":
        node = jsonio.load(path)
        claim_match = _CLAIM_ID.fullmatch(parsed.object_id)
        assert claim_match is not None
        expected_node = claim_match.group("node")
        if node.get("id") != expected_node:
            raise HtError(f"claim {canonical} containing node identity is inconsistent")
        claim = next(
            claim
            for claim in node.get("claims", [])
            if isinstance(claim, dict) and claim.get("id") == parsed.object_id
        )
        source_dispatch = claim.get("source_dispatch")
        if not isinstance(source_dispatch, str):
            raise HtError(f"claim {canonical} has no valid source_dispatch")
        source_match = _DISPATCH_ID.fullmatch(source_dispatch)
        if source_match is None or source_match.group("node") != expected_node:
            raise HtError(
                f"claim {canonical} source_dispatch {source_dispatch!r} is outside its node"
            )
        dispatch_path = root.dispatch_json(tree, expected_node, source_dispatch)
        if not _regular_file_in(root, dispatch_path, root.tree_dir(tree)):
            raise HtError(
                f"claim {canonical} names missing source dispatch {source_dispatch!r}"
            )
        dispatch = jsonio.load(dispatch_path)
        _verify_dispatch_identity(tree, source_dispatch, dispatch)
        return ResolvedRef(
            "claim",
            canonical,
            path,
            tree,
            parsed.object_id,
            _dispatch_issue_refs(dispatch),
            claim,
            {"node": node, "dispatch": dispatch},
        )
    header = load_adjudication_header(path)
    _verify_adjudication_header(root, tree, parsed.object_id, path, header)
    issue_ref = header.get("issue_ref")
    issue_refs = () if issue_ref is None else (issue_ref,)
    return ResolvedRef(
        "adjudication",
        canonical,
        path,
        tree,
        parsed.object_id,
        issue_refs,
        header,
    )


def _node_issue_refs(node: dict) -> tuple[str, ...]:
    minted_from = node.get("minted_from")
    return (minted_from,) if isinstance(minted_from, str) and _ISSUE.fullmatch(minted_from) else ()


def _dispatch_issue_refs(dispatch: dict) -> tuple[str, ...]:
    issue = canonical_issue_ref(dispatch.get("issue_ref"))
    return () if issue is None else (issue,)


def _verify_dispatch_identity(tree: str, dispatch_id: str, dispatch: dict) -> str:
    match = _DISPATCH_ID.fullmatch(dispatch_id)
    assert match is not None
    node_id = match.group("node")
    if dispatch.get("id") != dispatch_id or dispatch.get("node") != node_id:
        raise HtError(
            f"dispatch tree#{tree}/dispatch#{dispatch_id} path/document identity mismatch"
        )
    return node_id


def _verified_report(
    root: Root,
    tree: str,
    dispatch: dict,
    *,
    expected_dispatch_id: str | None = None,
    expected_node_id: str | None = None,
) -> tuple[Path, str]:
    dispatch_id = dispatch.get("id")
    node_id = dispatch.get("node")
    if not isinstance(dispatch_id, str) or _DISPATCH_ID.fullmatch(dispatch_id) is None:
        raise HtError(f"dispatch in tree {tree!r} has invalid id {dispatch_id!r}")
    parsed_node = _DISPATCH_ID.fullmatch(dispatch_id).group("node")
    if node_id != parsed_node:
        raise HtError(
            f"dispatch tree#{tree}/dispatch#{dispatch_id} node identity is inconsistent"
        )
    if expected_dispatch_id is not None and dispatch_id != expected_dispatch_id:
        raise HtError(
            f"dispatch identity {dispatch_id!r} does not match {expected_dispatch_id!r}"
        )
    if expected_node_id is not None and node_id != expected_node_id:
        raise HtError(f"dispatch {dispatch_id!r} is outside node {expected_node_id!r}")
    expected = root.reports_dir(tree, node_id) / f"{dispatch_id}-report.md"
    expected_ref = expected.absolute().relative_to(root.path.resolve()).as_posix()
    if dispatch.get("report_ref") != expected_ref:
        raise HtError(
            f"dispatch tree#{tree}/dispatch#{dispatch_id} report_ref "
            f"{dispatch.get('report_ref')!r} does not name its frozen report {expected_ref!r}"
        )
    if not _regular_file_in(root, expected, root.tree_dir(tree)):
        raise HtError(f"frozen report {expected_ref!r} is missing or not a regular file")
    digest = hashlib.sha256(expected.read_bytes()).hexdigest()
    if dispatch.get("report_hash") != digest:
        raise HtError(
            f"frozen report hash mismatch for tree#{tree}/report#{dispatch_id}: "
            f"{digest} != {dispatch.get('report_hash')!r}"
        )
    return expected, digest


def _resolve_ledger(root: Root, parsed: ParsedRef) -> ResolvedRef:
    matches: list[Path] = []
    if root.ledger_dir.is_dir() and not root.ledger_dir.is_symlink():
        for book in sorted(root.ledger_dir.iterdir(), key=lambda path: path.name):
            if book.is_symlink() or not book.is_dir():
                continue
            for section in ("user", "research", "observatory"):
                path = book / section / f"{parsed.object_id}.json"
                if _regular_file_in(root, path, book):
                    doc = jsonio.load(path)
                    if doc.get("id") != parsed.object_id:
                        raise HtError(
                            f"ledger path {root.rel(path)!r} contains mismatched id "
                            f"{doc.get('id')!r}"
                        )
                    matches.append(path)
    if not matches:
        raise HtUsageError(f"no such ledger entry '{parsed.object_id}'")
    if len(matches) > 1:
        locations = [root.rel(path) for path in matches]
        raise HtError(
            f"ledger id '{parsed.object_id}' is duplicated across the union at {locations}"
        )
    path = matches[0]
    return ResolvedRef(
        "ledger",
        parsed.canonical,
        path,
        None,
        parsed.object_id,
        (),
        jsonio.load(path),
        {"compatibility_alias": parsed.compatibility_alias},
    )


def _resolve_observatory(root: Root, parsed: ParsedRef) -> ResolvedRef:
    path = root.observatory_report_card(parsed.object_id)
    root_resolved = root.path.resolve()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HtUsageError(f"no such observatory report '{parsed.object_id}'") from exc
    if not resolved.is_relative_to(root_resolved):
        raise HtError(f"observatory report {parsed.canonical} escapes the research root")
    if resolved != path.absolute() or path.is_symlink() or not path.is_file():
        raise HtError(
            f"observatory report {parsed.canonical} must be the regular canonical report-card.md"
        )
    return ResolvedRef(
        "observatory-report",
        parsed.canonical,
        path,
        None,
        parsed.object_id,
        (),
        None,
        {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def parse_adjudication_header_line(first_line: str, source: str) -> dict:
    """Parse one reserved adjudication line with strict RFC-JSON semantics."""
    if not first_line.startswith(ADJUDICATION_HEADER_PREFIX):
        raise HtError(f"adjudication record {source} has no versioned JSON header")
    try:
        header = json.loads(
            first_line[len(ADJUDICATION_HEADER_PREFIX) :],
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise HtError(
            f"adjudication record {source} has invalid JSON header: {exc}"
        ) from exc
    if not isinstance(header, dict):
        raise HtError(f"adjudication record {source} JSON header must be an object")
    return header


def load_adjudication_header(path: Path) -> dict:
    try:
        first_line = path.open("r", encoding="utf-8").readline().rstrip("\n")
    except (OSError, UnicodeError) as exc:
        raise HtError(f"cannot read adjudication record {path}: {exc}") from exc
    return parse_adjudication_header_line(first_line, str(path))


def _require_nonempty_string(header: dict, field: str, canonical_adj: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value:
        raise HtError(
            f"adjudication {canonical_adj} header {field} must be a non-empty string"
        )
    return value


def _require_epoch(header: dict, canonical_adj: str) -> int:
    value = header.get("epoch")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HtError(
            f"adjudication {canonical_adj} header epoch must be a non-negative integer"
        )
    return value


def _verify_adjudication_header(
    root: Root,
    tree: str,
    adjudication_id: str,
    path: Path,
    header: dict,
) -> None:
    adj_match = _ADJUDICATION_ID.fullmatch(adjudication_id)
    assert adj_match is not None
    dispatch_id = adj_match.group("dispatch")
    dispatch_match = _DISPATCH_ID.fullmatch(dispatch_id)
    assert dispatch_match is not None
    node_id = dispatch_match.group("node")
    canonical_adj = f"tree#{tree}/adjudication#{adjudication_id}"
    expected = {
        "schema_version": "ht-adjudication/1.0.0",
        "adjudication_ref": canonical_adj,
        "dispatch_ref": f"tree#{tree}/dispatch#{dispatch_id}",
        "node_ref": f"tree#{tree}/node#{node_id}",
        "report_ref": f"tree#{tree}/report#{dispatch_id}",
    }
    for field, value in expected.items():
        if header.get(field) != value:
            raise HtError(
                f"adjudication {canonical_adj} header {field} is "
                f"{header.get(field)!r}, requires {value!r}"
            )
    if not _regular_file_in(root, path, root.tree_dir(tree)):
        raise HtError(f"adjudication {canonical_adj} is not a confined regular file")
    dispatch_path = root.dispatch_json(tree, node_id, dispatch_id)
    if not _regular_file_in(root, dispatch_path, root.tree_dir(tree)):
        raise HtError(f"adjudication {canonical_adj} names a missing dispatch")
    dispatch = jsonio.load(dispatch_path)
    _verify_dispatch_identity(tree, dispatch_id, dispatch)
    history_matches = []
    for ref in dispatch.get("adjudications", []):
        if not isinstance(ref, str):
            continue
        try:
            parsed_history = parse_ref(ref)
        except HtUsageError:
            continue
        if (
            parsed_history.kind == "adjudication"
            and parsed_history.canonical == canonical_adj
        ):
            history_matches.append(ref)
    if len(history_matches) != 1:
        raise HtError(
            f"adjudication {canonical_adj} is not a member of its source dispatch history"
        )
    _report_path, report_hash = _verified_report(
        root,
        tree,
        dispatch,
        expected_dispatch_id=dispatch_id,
        expected_node_id=node_id,
    )
    if header.get("report_sha256") != report_hash:
        raise HtError(f"adjudication {canonical_adj} report hash does not match dispatch")
    expected_issue = canonical_issue_ref(dispatch.get("issue_ref"))
    if header.get("issue_ref") != expected_issue:
        raise HtError(f"adjudication {canonical_adj} issue affiliation does not match dispatch")
    if path.absolute() != root.adjudication_path(tree, node_id, adjudication_id).absolute():
        raise HtError(f"adjudication {canonical_adj} is outside its canonical node directory")

    verdict = header.get("verdict")
    if verdict not in {"granted", "demoted", "rejected"}:
        raise HtError(f"adjudication {canonical_adj} has invalid verdict {verdict!r}")
    epoch = _require_epoch(header, canonical_adj)
    _require_nonempty_string(header, "date", canonical_adj)
    reason = header.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HtError(f"adjudication {canonical_adj} reason must be a string or null")

    claim_ref = header.get("claim_ref")
    proposed_tier = header.get("proposed_tier")
    granted_tier = header.get("granted_tier")
    if verdict == "rejected":
        if claim_ref is not None or proposed_tier is not None or granted_tier is not None:
            raise HtError(
                f"rejected adjudication {canonical_adj} must have null claim and tiers"
            )
        if not isinstance(reason, str) or not reason:
            raise HtError(f"rejected adjudication {canonical_adj} requires a reason")
        return

    if not isinstance(claim_ref, str):
        raise HtError(f"adjudication {canonical_adj} requires a canonical claim_ref")
    parsed_claim = parse_ref(claim_ref)
    if (
        parsed_claim.kind != "claim"
        or parsed_claim.local
        or parsed_claim.tree != tree
    ):
        raise HtError(
            f"adjudication {canonical_adj} claim_ref {claim_ref!r} is not canonical in tree {tree}"
        )
    claim_match = _CLAIM_ID.fullmatch(parsed_claim.object_id)
    assert claim_match is not None
    if claim_match.group("node") != node_id:
        raise HtError(f"adjudication {canonical_adj} claim_ref belongs to another node")
    node_path = root.node_json(tree, node_id)
    if not _regular_file_in(root, node_path, root.tree_dir(tree)):
        raise HtError(f"adjudication {canonical_adj} names a missing containing node")
    node = jsonio.load(node_path)
    if node.get("id") != node_id:
        raise HtError(f"adjudication {canonical_adj} containing node identity is invalid")
    claims = [
        claim
        for claim in node.get("claims", [])
        if isinstance(claim, dict) and claim.get("id") == parsed_claim.object_id
    ]
    if len(claims) != 1:
        raise HtError(
            f"adjudication {canonical_adj} claim_ref does not name exactly one embedded claim"
        )
    claim = claims[0]
    if (
        claim.get("node") != node_id
        or claim.get("source_dispatch") != dispatch_id
    ):
        raise HtError(
            f"adjudication {canonical_adj} claim identity/source dispatch is inconsistent"
        )
    if (
        header.get("proposed_tier") != claim.get("proposed_tier")
        or header.get("granted_tier") != claim.get("granted_tier")
        or epoch != claim.get("epoch")
    ):
        raise HtError(
            f"adjudication {canonical_adj} tier/epoch fields do not match its embedded claim"
        )
    proposed_tier = claim.get("proposed_tier")
    granted_tier = claim.get("granted_tier")
    if (
        not isinstance(proposed_tier, int)
        or isinstance(proposed_tier, bool)
        or not isinstance(granted_tier, int)
        or isinstance(granted_tier, bool)
    ):
        raise HtError(f"adjudication {canonical_adj} claim tiers are invalid")
    expected_verdict = "granted" if granted_tier == proposed_tier else "demoted"
    if granted_tier > proposed_tier or verdict != expected_verdict:
        raise HtError(
            f"adjudication {canonical_adj} verdict is incoherent with its claim tiers"
        )
    if verdict == "demoted" and (not isinstance(reason, str) or not reason):
        raise HtError(f"demoted adjudication {canonical_adj} requires a reason")


def _resolve_merge_record(root: Root, parsed: ParsedRef) -> ResolvedRef:
    path = root.merge_record_json(parsed.object_id)
    if not _regular_file_in(root, path, root.tier1_dir / "merge-records"):
        raise HtUsageError(f"no such merge record '{parsed.object_id}'")
    record = jsonio.load(path)
    if record.get("id") != parsed.object_id:
        raise HtError(
            f"merge-record path {root.rel(path)!r} contains id {record.get('id')!r}, "
            f"requires {parsed.object_id!r}"
        )
    issue_refs: set[str] = set()
    candidate = record.get("candidate_ref")
    if not isinstance(candidate, str):
        raise HtError(f"merge record {parsed.object_id} has malformed candidate_ref")
    resolved_candidate = resolve_ref(root, candidate, expected={"node"})
    if candidate != resolved_candidate.canonical:
        raise HtError(f"merge record {parsed.object_id} candidate_ref is not canonical")
    issue_refs.update(resolved_candidate.issue_refs)
    has_lane = "lane_adjudication_ref" in record
    has_backing = "backing_claims" in record
    if has_lane != has_backing:
        raise HtError(
            f"merge record {parsed.object_id} has a broken partial provenance anchor"
        )
    lane_adj = record.get("lane_adjudication_ref")
    if has_lane:
        if not isinstance(lane_adj, str):
            raise HtError(
                f"merge record {parsed.object_id} has malformed lane_adjudication_ref"
            )
        lane = resolve_ref(root, lane_adj, expected={"adjudication"})
        if (
            lane_adj != lane.canonical
            or lane.tree != resolved_candidate.tree
            or lane.document.get("node_ref") != resolved_candidate.canonical
        ):
            raise HtError(
                f"merge record {parsed.object_id} lane adjudication is outside its candidate"
            )
        issue_refs.update(lane.issue_refs)
    backing = record.get("backing_claims")
    if has_backing:
        if not isinstance(backing, list) or not backing:
            raise HtError(f"merge record {parsed.object_id} has malformed backing_claims")
        for item in backing:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("ref"), str)
                or not isinstance(item.get("adjudication_ref"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) is None
            ):
                raise HtError(f"merge record {parsed.object_id} has malformed backing_claims")
            claim = resolve_ref(root, item["ref"], expected={"claim"})
            adjudication = resolve_ref(
                root, item["adjudication_ref"], expected={"adjudication"}
            )
            if item["ref"] != claim.canonical or item["adjudication_ref"] != adjudication.canonical:
                raise HtError(
                    f"merge record {parsed.object_id} backing refs must be canonical"
                )
            if (
                claim.tree != resolved_candidate.tree
                or claim.document.get("node") != resolved_candidate.object_id
                or adjudication.tree != resolved_candidate.tree
                or adjudication.document.get("node_ref") != resolved_candidate.canonical
            ):
                raise HtError(
                    f"merge record {parsed.object_id} backing evidence is outside its candidate"
                )
            if canonical_json_sha256(claim.document) != item["sha256"]:
                raise HtError(
                    f"merge record {parsed.object_id} backing claim {claim.canonical} hash drifted"
                )
            if adjudication.document.get("claim_ref") != claim.canonical:
                raise HtError(
                    f"merge record {parsed.object_id} backing adjudication does not name "
                    f"{claim.canonical}"
                )
            issue_refs.update(claim.issue_refs)
            issue_refs.update(adjudication.issue_refs)
        backing_refs = {item["ref"] for item in backing}
        if has_lane and lane.document.get("claim_ref") not in backing_refs:
            raise HtError(
                f"merge record {parsed.object_id} lane adjudication claim is not a backing claim"
            )
    if len(issue_refs) > 1:
        raise HtError(
            f"merge record {parsed.object_id} has conflicting issue affiliations: "
            f"{', '.join(sorted(issue_refs))}"
        )
    return ResolvedRef(
        "merge-record",
        parsed.canonical,
        path,
        None,
        parsed.object_id,
        tuple(sorted(issue_refs)),
        record,
    )
