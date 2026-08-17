"""Tier-1 issue lifecycle commands owned by the principal coordinator.

Phase-dependent ratification/activation authority remains single-sourced in
``authority.check_write``.  This module builds the requested issue mutation and
supplies issue-specific lifecycle checks, capacity warnings, and closure scans.
"""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path

from .. import gitutil, jsonio
from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan
from ..references import (
    parse_ref,
    resolve_for_issue,
    resolve_ref,
)
from ._common import Ctx


_ISSUE_ID = re.compile(r"I-[0-9]+")
_LEVEL_LANE = re.compile(r"L([1-5])\+?")
_DISPATCH_PATH = re.compile(
    r"trees/(?P<component>[^/#\s]+)/nodes/"
    r"(?P<node>[1-9][0-9]*(?:\.[1-9][0-9]*)*)/dispatches/"
    r"(?P<dispatch>d-(?P=node)-[1-9][0-9]*)\.json",
    re.ASCII,
)
_SOURCE_REF = re.compile(
    r"(?:ledger(?:-entry)?#L-[0-9]+|"
    r"(?:observatory(?:-finding)?|observation)#[^\s#]+|"
    r"user-seed#[^\s#]+)"
)
_TERMINAL_DISPATCH_OUTCOMES = frozenset({"completed", "recalled"})
_STATUS_PREDECESSORS = {
    "ratified": frozenset({"proposed"}),
    "active": frozenset({"ratified", "parked"}),
    "parked": frozenset({"active"}),
    "withdrawn": frozenset({"proposed", "active", "parked"}),
}

CAPACITY_WARNING_POLICY = (
    "Capacity-1 is warn-only in v1. If live use shows the warning being ignored, "
    "surface that as evidence for a machinery upgrade; never change this policy "
    "silently in either direction."
)


def _issue_paths(ctx: Ctx) -> list[Path]:
    directory = ctx.root.tier1_dir / "issues"
    return sorted(directory.glob("I-*.json")) if directory.is_dir() else []


def load_issue(ctx: Ctx, issue_id: str) -> dict:
    if not isinstance(issue_id, str) or _ISSUE_ID.fullmatch(issue_id) is None:
        raise HtUsageError("issue id must have the form I-<n>")
    path = ctx.root.issue_json(issue_id)
    if not path.exists():
        raise HtUsageError(f"no such issue '{issue_id}'")
    return jsonio.load(path)


def _resolve_target_issue(ctx: Ctx, issue_id: str):
    if not isinstance(issue_id, str) or _ISSUE_ID.fullmatch(issue_id) is None:
        raise HtUsageError("issue id must have the form I-<n>")
    resolved = resolve_ref(
        ctx.root,
        f"issue#{issue_id}",
        expected={"issue"},
    )
    if (
        resolved.object_id != issue_id
        or resolved.path != ctx.root.issue_json(issue_id)
    ):
        raise HtError(f"issue {issue_id} did not resolve to its canonical path")
    return resolved


def _recheck_target_issue(ctx: Ctx, canonical: str, path: Path, old: dict) -> None:
    current = resolve_ref(ctx.root, canonical, expected={"issue"})
    if current.path != path or current.document != old:
        raise HtError(f"issue {canonical} changed before commit")


def next_issue_id(ctx: Ctx) -> str:
    """Allocate the union-global issue id while the caller holds the mutex."""
    numbers = []
    for path in _issue_paths(ctx):
        match = re.fullmatch(r"I-([0-9]+)\.json", path.name)
        if match is not None:
            numbers.append(int(match.group(1)))
    return f"I-{max(numbers, default=0) + 1}"


def compute_scope(lanes: list[str]) -> str:
    """Return the L1-L5 chain LCA (the minimum-numbered implicated level)."""
    if not lanes:
        raise HtUsageError("issue mint requires at least one --lanes value")
    levels: list[int] = []
    invalid: list[str] = []
    for lane in lanes:
        match = _LEVEL_LANE.fullmatch(lane.strip()) if isinstance(lane, str) else None
        if match is None:
            invalid.append(str(lane))
        else:
            levels.append(int(match.group(1)))
    if invalid:
        raise HtError(
            "cannot compute issue scope from non-L1-L5 lanes: "
            f"{', '.join(invalid)}; scope was not guessed (item 2 X4)"
        )
    return f"L{min(levels)}"


def _validate_mint_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HtUsageError(f"issue mint requires non-empty --{name}")


def mint(
    ctx: Ctx,
    title: str,
    question: str,
    done_definition: str,
    provenance: list[str],
    lanes: list[str],
) -> Plan:
    _validate_mint_text("title", title)
    _validate_mint_text("question", question)
    _validate_mint_text("done-definition", done_definition)
    if not provenance or any(not isinstance(ref, str) or not ref.strip() for ref in provenance):
        raise HtUsageError("issue mint requires one or more non-empty --provenance refs")
    if not any(_SOURCE_REF.fullmatch(ref.strip()) for ref in provenance):
        raise HtError(
            "issue mint provenance must include at least one ledger-entry, "
            "observatory, or user-seed ref (coherence amendments §13)"
        )

    issue_id = next_issue_id(ctx)
    issue = {
        "id": issue_id,
        "title": title,
        "provenance": provenance,
        "scope": compute_scope(lanes),
        "question": question,
        "done_definition": done_definition,
        "lanes": lanes,
        "subgoals": [],
        "observatory_attachments": [],
        "status": "proposed",
        "closure": None,
    }
    return Plan(
        role=ctx.role,
        message=f"ht issue mint: {issue_id}",
        writes=[DocWrite(ctx.root.issue_json(issue_id), "issue", None, issue)],
    )


def _status(ctx: Ctx, issue_id: str, target: str) -> Plan:
    old = load_issue(ctx, issue_id)
    _ensure_not_closed(old)
    allowed = _STATUS_PREDECESSORS[target]
    if old.get("status") not in allowed:
        expected = "|".join(sorted(allowed))
        raise HtError(
            f"cannot set issue {issue_id} to {target}: status is "
            f"'{old.get('status')}', requires {expected} (PC note §3)"
        )
    new = dict(old)
    new["status"] = target
    warnings: list[str] = []
    if target == "active":
        for path in _issue_paths(ctx):
            other = jsonio.load(path)
            if other.get("id") != issue_id and other.get("status") == "active":
                warnings.append(
                    "CAPACITY-1 WARNING: activating "
                    f"{issue_id} while blocking issue {other['id']} "
                    f"({other['title']}) is already active; activation is warn-only "
                    "in v1 (PC note §3; D7 exception)."
                )
    return Plan(
        role=ctx.role,
        message=f"ht issue {target}: {issue_id}",
        writes=[DocWrite(ctx.root.issue_json(issue_id), "issue", old, new)],
        warnings=warnings,
    )


def ratify(ctx: Ctx, issue_id: str) -> Plan:
    return _status(ctx, issue_id, "ratified")


def activate(ctx: Ctx, issue_id: str) -> Plan:
    return _status(ctx, issue_id, "active")


def park(ctx: Ctx, issue_id: str) -> Plan:
    return _status(ctx, issue_id, "parked")


def withdraw(ctx: Ctx, issue_id: str) -> Plan:
    return _status(ctx, issue_id, "withdrawn")


def subgoal_add(ctx: Ctx, issue_id: str, ref: str) -> Plan:
    if not ref.strip():
        raise HtUsageError("issue subgoal-add requires a non-empty --ref")
    target = _resolve_target_issue(ctx, issue_id)
    assert target.path is not None
    old = target.document
    _ensure_not_closed(old)
    resolved = resolve_for_issue(
        ctx.root,
        ref,
        issue_id,
        expected={"node", "dispatch"},
    )
    new = dict(old)
    new["subgoals"] = list(old.get("subgoals", [])) + [resolved.canonical]

    def unchanged() -> None:
        _recheck_target_issue(ctx, target.canonical, target.path, old)
        current = resolve_for_issue(
            ctx.root,
            resolved.canonical,
            issue_id,
            expected={"node", "dispatch"},
        )
        if current.canonical != resolved.canonical:
            raise HtError(f"subgoal {resolved.canonical} changed before commit")

    return Plan(
        role=ctx.role,
        message=f"ht issue subgoal-add: {issue_id} <- {resolved.canonical}",
        writes=[DocWrite(target.path, "issue", old, new)],
        semantic=unchanged,
    )


def _stable_bytes(path: Path) -> tuple[bytes, tuple[int, int, int, int, int]]:
    try:
        before = path.stat()
        if not path.is_file() or path.is_symlink():
            raise HtUsageError(f"artifact '{path}' must be a regular non-symlink file")
        content = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise HtUsageError(f"cannot read artifact '{path}': {exc}") from exc
    before_fp = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_fp = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_fp != after_fp or len(content) != after.st_size:
        raise HtError(f"artifact '{path}' changed during stable read; retry")
    return content, after_fp


def _require_exact_committed_blob_identity(
    ctx: Ctx,
    relative: str,
    subject: str,
) -> str:
    """Prove one working path is the exact non-executable HEAD/index blob."""
    path = ctx.root.path / relative
    try:
        working = path.lstat()
    except OSError as exc:
        raise HtError(f"{subject} cannot be inspected: {exc}") from exc
    if (
        not stat.S_ISREG(working.st_mode)
        or stat.S_ISLNK(working.st_mode)
        or working.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        raise HtError(
            f"{subject} working path must be a non-executable "
            "regular non-symlink file"
        )

    indexed = gitutil.run(
        ctx.root.path,
        ["ls-files", "--stage", "-z", "--", relative],
        check=False,
    )
    rows = [row for row in indexed.stdout.split("\x00") if row]
    header, separator, indexed_path = (
        rows[0].partition("\t") if len(rows) == 1 else ("", "", "")
    )
    fields = header.split()
    if (
        indexed.returncode != 0
        or len(rows) != 1
        or not separator
        or indexed_path != relative
        or len(fields) != 3
        or fields[0] != "100644"
        or fields[2] != "0"
    ):
        raise HtError(
            f"{subject} index entry must be exactly one "
            "stage-0 100644 blob"
        )
    index_oid = fields[1]
    index_type = gitutil.run(
        ctx.root.path,
        ["cat-file", "-t", index_oid],
        check=False,
    )
    if index_type.returncode != 0 or index_type.stdout.strip() != "blob":
        raise HtError(f"{subject} index object must be a blob")

    head = gitutil.run(
        ctx.root.path,
        ["ls-tree", "-z", "HEAD", "--", relative],
        check=False,
    )
    head_rows = [row for row in head.stdout.split("\x00") if row]
    head_header, head_separator, head_path = (
        head_rows[0].partition("\t") if len(head_rows) == 1 else ("", "", "")
    )
    head_fields = head_header.split()
    if (
        head.returncode != 0
        or len(head_rows) != 1
        or not head_separator
        or head_path != relative
        or len(head_fields) != 3
        or head_fields[0] != "100644"
        or head_fields[1] != "blob"
    ):
        raise HtError(
            f"{subject} HEAD entry must be exactly one 100644 blob"
        )
    head_oid = head_fields[2]
    current = gitutil.run(
        ctx.root.path,
        ["hash-object", "--no-filters", "--", relative],
        check=False,
    )
    current_oid = current.stdout.strip()
    if (
        current.returncode != 0
        or not current_oid
        or current_oid != index_oid
        or index_oid != head_oid
    ):
        raise HtError(
            f"{subject} must have identical working, index, and committed Git blob "
            "identities"
        )
    return head_oid


def observatory_attach(ctx: Ctx, issue_id: str, ref: str) -> Plan:
    target = _resolve_target_issue(ctx, issue_id)
    assert target.path is not None
    old = target.document
    _ensure_not_closed(old)
    resolved = resolve_ref(ctx.root, ref, expected={"observatory-report"})
    assert resolved.path is not None
    relative = ctx.root.rel(resolved.path)
    committed_oid = _require_exact_committed_blob_identity(
        ctx,
        relative,
        f"observatory report {resolved.canonical}",
    )
    content, fingerprint = _stable_bytes(resolved.path)
    digest = hashlib.sha256(content).hexdigest()
    attachments = list(old.get("observatory_attachments", []))
    if any(item.get("ref") == resolved.canonical for item in attachments):
        raise HtError(
            f"issue {issue_id} already has observatory attachment {resolved.canonical}"
        )
    new = dict(old)
    new["observatory_attachments"] = attachments + [
        {"ref": resolved.canonical, "sha256": digest}
    ]

    def unchanged() -> None:
        _recheck_target_issue(ctx, target.canonical, target.path, old)
        _content, current = _stable_bytes(resolved.path)
        if current != fingerprint or hashlib.sha256(_content).hexdigest() != digest:
            raise HtError(
                f"observatory report {resolved.canonical} changed before attachment commit"
            )
        if _require_exact_committed_blob_identity(
            ctx,
            relative,
            f"observatory report {resolved.canonical}",
        ) != committed_oid:
            raise HtError(
                f"observatory report {resolved.canonical} no longer matches its HEAD blob"
            )

    return Plan(
        role=ctx.role,
        message=f"ht issue observatory-attach: {issue_id} <- {resolved.canonical}",
        writes=[DocWrite(target.path, "issue", old, new)],
        semantic=unchanged,
    )


def _dispatch_path_identity(relative: str) -> tuple[str, str, str] | None:
    """Return canonical dispatch path identity, rejecting malformed lane entries."""
    parts = relative.split("/")
    if not (
        len(parts) >= 5
        and parts[0] == "trees"
        and parts[2] == "nodes"
        and parts[4] == "dispatches"
    ):
        return None
    if len(parts) == 6 and parts[-1].startswith("."):
        return None
    match = _DISPATCH_PATH.fullmatch(relative)
    if match is None:
        raise HtError(f"dispatch entry '{relative}' is not a canonical dispatch path")
    return match.group("component"), match.group("node"), match.group("dispatch")


def _working_dispatch_paths(ctx: Ctx) -> set[str]:
    paths: set[str] = set()
    trees = ctx.root.trees_dir
    if trees.is_symlink() or not trees.is_dir():
        raise HtError("trees lane must be a canonical non-symlink directory")
    for tree_dir in sorted(trees.iterdir(), key=lambda item: item.name):
        if tree_dir.name.startswith("."):
            continue
        if tree_dir.is_symlink() or not tree_dir.is_dir():
            raise HtError(f"tree entry {tree_dir} must be a canonical directory")
        component = tree_dir.name
        nodes_dir = tree_dir / "nodes"
        if nodes_dir.is_symlink() or not nodes_dir.is_dir():
            raise HtError(f"tree {component} nodes lane must be a canonical directory")
        for node_dir in sorted(nodes_dir.iterdir(), key=lambda item: item.name):
            if node_dir.name.startswith("."):
                continue
            if node_dir.is_symlink() or not node_dir.is_dir():
                raise HtError(f"node entry {node_dir} must be a canonical directory")
            dispatches_dir = node_dir / "dispatches"
            if dispatches_dir.is_symlink() or not dispatches_dir.is_dir():
                if not dispatches_dir.exists() and not dispatches_dir.is_symlink():
                    continue
                raise HtError(
                    f"dispatch lane for tree#{component}/node#{node_dir.name} "
                    "must be a canonical directory"
                )
            for path in sorted(dispatches_dir.iterdir(), key=lambda item: item.name):
                if path.name.startswith("."):
                    continue
                if path.is_symlink() or not path.is_file():
                    raise HtError(f"dispatch entry {path} is not a canonical JSON file")
                relative = ctx.root.rel(path)
                identity = _dispatch_path_identity(relative)
                if identity != (component, node_dir.name, path.stem):
                    raise HtError(f"dispatch entry '{relative}' has inconsistent path identity")
                paths.add(relative)
    return paths


def _index_dispatch_paths(ctx: Ctx) -> set[str]:
    indexed = gitutil.run(
        ctx.root.path,
        ["ls-files", "--stage", "-z", "--", "trees"],
        check=False,
    )
    if indexed.returncode != 0:
        raise HtError("cannot enumerate the exact stage-0 dispatch cohort")
    paths: set[str] = set()
    for row in (value for value in indexed.stdout.split("\x00") if value):
        header, separator, relative = row.partition("\t")
        if not separator:
            raise HtError("malformed Git index record while enumerating dispatch cohort")
        identity = _dispatch_path_identity(relative)
        if identity is None:
            continue
        fields = header.split()
        if len(fields) != 3 or fields[2] != "0" or relative in paths:
            raise HtError(
                f"dispatch cohort path '{relative}' must have exactly one stage-0 index entry"
            )
        paths.add(relative)
    return paths


def _head_dispatch_paths(ctx: Ctx) -> set[str]:
    committed = gitutil.run(
        ctx.root.path,
        ["ls-tree", "-r", "-z", "HEAD", "--", "trees"],
        check=False,
    )
    if committed.returncode != 0:
        raise HtError("cannot enumerate the committed HEAD dispatch cohort")
    paths: set[str] = set()
    for row in (value for value in committed.stdout.split("\x00") if value):
        header, separator, relative = row.partition("\t")
        if not separator or len(header.split()) != 3:
            raise HtError("malformed HEAD tree record while enumerating dispatch cohort")
        identity = _dispatch_path_identity(relative)
        if identity is None:
            continue
        if relative in paths:
            raise HtError(f"duplicate committed dispatch cohort path '{relative}'")
        paths.add(relative)
    return paths


def _exact_dispatch_cohort_paths(ctx: Ctx) -> list[str]:
    working = _working_dispatch_paths(ctx)
    indexed = _index_dispatch_paths(ctx)
    committed = _head_dispatch_paths(ctx)
    if working != indexed or indexed != committed:
        details = []
        for name, values in (
            ("working-only", working - indexed),
            ("index-only", indexed - working),
            ("index-not-HEAD", indexed - committed),
            ("HEAD-not-index", committed - indexed),
        ):
            if values:
                details.append(f"{name}={','.join(sorted(values))}")
        raise HtError(
            "issue close dispatch cohort is not the exact committed cohort: "
            + "; ".join(details)
        )
    return sorted(working)


def _issue_dispatches(ctx: Ctx, issue_id: str) -> list[tuple[str, dict, dict]]:
    matches: list[tuple[str, dict, dict]] = []
    # Load-bearing: closure enrollment keys ONLY on dispatch.issue_ref, never on
    # issue.subgoals.  The complete working/index/HEAD path set is proved before
    # any document can influence affiliation or terminality.
    for relative in _exact_dispatch_cohort_paths(ctx):
        identity = _dispatch_path_identity(relative)
        assert identity is not None
        component, node_id, dispatch_id = identity
        canonical = f"tree#{component}/dispatch#{dispatch_id}"
        _require_exact_committed_blob_identity(
            ctx,
            relative,
            f"dispatch cohort member {canonical}",
        )
        path = ctx.root.path / relative
        dispatch_resolved = resolve_ref(ctx.root, canonical, expected={"dispatch"})
        if (
            dispatch_resolved.canonical != canonical
            or dispatch_resolved.path != path
            or dispatch_resolved.tree != component
            or dispatch_resolved.object_id != dispatch_id
        ):
            raise HtError(f"dispatch {canonical} did not resolve to its canonical identity")
        if dispatch_resolved.issue_refs != (f"issue#{issue_id}",):
            continue

        node_ref = f"tree#{component}/node#{node_id}"
        node_path = ctx.root.node_json(component, node_id)
        if dispatch_resolved.document.get("outcome") not in _TERMINAL_DISPATCH_OUTCOMES:
            _require_exact_committed_blob_identity(
                ctx,
                ctx.root.rel(node_path),
                f"demotion-source node {node_ref}",
            )
        node_resolved = resolve_ref(ctx.root, node_ref, expected={"node"})
        if (
            node_resolved.canonical != node_ref
            or node_resolved.path != node_path
            or node_resolved.tree != component
            or node_resolved.object_id != node_id
        ):
            raise HtError(f"node {node_ref} did not resolve to its canonical identity")
        matches.append(
            (
                dispatch_resolved.canonical,
                dispatch_resolved.document,
                node_resolved.document,
            )
        )
    return matches


def _node_is_demoted(node: dict) -> bool:
    """Map G1's demoted terminal to the latest node conflict settlement.

    ``node.status`` has no ``demoted`` value: the settle command writes status
    ``closed`` and records the ruled demotion in the latest conflict row.
    """
    conflicts = node.get("conflicts") or []
    return bool(conflicts) and conflicts[-1].get("settlement") == "demoted"


def _received_interrupt_ids(ctx: Ctx) -> set[str]:
    received: set[str] = set()
    directory = ctx.root.tier1_dir / "decision-log"
    if not directory.is_dir():
        return received
    for path in sorted(directory.glob("PCD-*.json")):
        decision = jsonio.load(path)
        if decision.get("kind") != "interrupt-receipt":
            continue
        ref = decision.get("ref")
        if isinstance(ref, str):
            received.add(ref)
        received.update(
            value
            for value in decision.get("context_refs", [])
            if isinstance(value, str) and re.fullmatch(r"INT-[0-9]+", value)
        )
    return received


def _open_interrupt_ids(ctx: Ctx, issue: dict) -> list[str]:
    received = _received_interrupt_ids(ctx)
    subgoals = set(issue.get("subgoals", []))
    blocking = []
    directory = ctx.root.tier1_dir / "interrupts"
    if not directory.is_dir():
        return blocking
    for path in sorted(directory.glob("INT-*.json")):
        interrupt = jsonio.load(path)
        concerns_issue = interrupt.get("issue_ref") == issue.get("id")
        concerns_subgoal = interrupt.get("sub_goal_ref") in subgoals
        if (concerns_issue or concerns_subgoal) and interrupt.get("id") not in received:
            blocking.append(str(interrupt.get("id", path.stem)))
    return blocking


def close(
    ctx: Ctx,
    issue_id: str,
    text: str,
    refs: list[str] | None = None,
) -> Plan:
    if not text.strip():
        raise HtUsageError("issue close requires non-empty closure text")
    target = _resolve_target_issue(ctx, issue_id)
    assert target.path is not None
    old = target.document
    _ensure_not_closed(old)
    canonical_refs = _closure_refs(ctx, old, list(refs or []))
    if old.get("status") != "active":
        raise HtError(
            f"cannot close issue {issue_id}: status is '{old.get('status')}', "
            "requires active (PC note §3)"
        )
    open_interrupts = _open_interrupt_ids(ctx, old)
    if open_interrupts:
        raise HtError(
            f"cannot close issue {issue_id}: undispositioned interrupts: "
            f"{', '.join(open_interrupts)}; record each with ht pcd append "
            "--kind interrupt-receipt --ref INT-<n> (F11)"
        )
    dispatches = _issue_dispatches(ctx, issue_id)
    nonterminal = [
        (dispatch_ref, dispatch)
        for dispatch_ref, dispatch, node in dispatches
        if dispatch.get("outcome") not in _TERMINAL_DISPATCH_OUTCOMES
        and not _node_is_demoted(node)
    ]
    if nonterminal:
        details = ", ".join(
            f"{label} ({dispatch.get('outcome') or 'pending'})"
            for label, dispatch in nonterminal
        )
        raise HtError(
            f"cannot close issue {issue_id}: non-terminal sub-dispatches: {details} "
            "(coherence amendments §6)"
        )

    dispositions = []
    for dispatch_ref, dispatch, node in dispatches:
        if dispatch.get("outcome") in _TERMINAL_DISPATCH_OUTCOMES:
            dispositions.append({"ref": dispatch_ref, "status": dispatch["outcome"]})
        elif _node_is_demoted(node):
            dispositions.append({"ref": dispatch_ref, "status": "demoted"})
    new = dict(old)
    new["status"] = "closed"
    new["closure"] = {
        "text": text,
        "refs": canonical_refs,
        "dispositions": dispositions,
    }

    def unchanged() -> None:
        _recheck_target_issue(ctx, target.canonical, target.path, old)
        if _issue_dispatches(ctx, issue_id) != dispatches:
            raise HtError(f"issue {issue_id} dispatch cohort changed before close commit")

    return Plan(
        role=ctx.role,
        message=f"ht issue close: {issue_id}",
        writes=[DocWrite(target.path, "issue", old, new)],
        semantic=unchanged,
    )


_CLOSURE_KINDS = frozenset(
    {
        "issue",
        "ledger",
        "node",
        "dispatch",
        "report",
        "claim",
        "adjudication",
        "merge-record",
        "observatory-report",
    }
)


def _canonical_provenance_refs(issue: dict) -> set[str]:
    canonical: set[str] = set()
    for value in issue.get("provenance", []):
        if not isinstance(value, str):
            continue
        try:
            parsed = parse_ref(value)
        except HtUsageError:
            continue
        canonical.add(parsed.canonical)
    return canonical


def _closure_refs(ctx: Ctx, issue: dict, refs: list[str]) -> list[str]:
    issue_id = issue["id"]
    provenance = _canonical_provenance_refs(issue)
    attachments = {
        item.get("ref"): item.get("sha256")
        for item in issue.get("observatory_attachments", [])
        if isinstance(item, dict)
    }
    canonical: list[str] = []
    for value in refs:
        resolved = resolve_ref(ctx.root, value, expected=_CLOSURE_KINDS)
        if resolved.kind == "ledger":
            if resolved.canonical not in provenance:
                raise HtError(
                    f"ledger ref {resolved.canonical} is not present in issue "
                    f"{issue_id} provenance"
                )
        elif resolved.kind == "observatory-report":
            expected_hash = attachments.get(resolved.canonical)
            actual_hash = (resolved.metadata or {}).get("sha256")
            if expected_hash is None or expected_hash != actual_hash:
                raise HtError(
                    f"observatory ref {resolved.canonical} is not attached to issue "
                    f"{issue_id} with its exact current hash"
                )
        else:
            resolved = resolve_for_issue(
                ctx.root,
                value,
                issue_id,
                expected=_CLOSURE_KINDS - {"ledger", "observatory-report"},
            )
        canonical.append(resolved.canonical)
    return canonical


def _ensure_not_closed(issue: dict) -> None:
    if issue.get("status") == "closed":
        raise HtError(
            f"issue {issue.get('id')} is closed and immutable "
            "(A1 §10 write-authority; D10)"
        )
