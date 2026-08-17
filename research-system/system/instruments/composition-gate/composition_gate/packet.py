"""Deterministic, committed-snapshot packet preparation for stage-2 review.

This module has no verdict or Tier-1 mutation surface.  A caller supplies one
decision snapshot and one research root; all input bytes come from physical Git
objects at that snapshot, while all output is confined to a fresh, exclusive
attempt directory below the supplied root's ignored ``var/`` lane.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Iterable

from .discovery import Discovery, GitEntry, GitSnapshot, ScreenInputError, _run_git_binary
from .normalization import comparison_key, stable_path_key, stable_text_key


PACKET_FORMAT = "composition-gate-packet.v1"
MANIFEST_FORMAT = "composition-gate-packet-manifest.v1"
PROMPT_NAME = "composition-gate-review.v1.md"
# Frozen by tests against the exact source bytes in ../prompts/.
PROMPT_SHA256 = "0532c865cc1b8b06898fe3b735ab043ec76aa465c2b94535e9c4bb4d4e0cdf04"

_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
_RECORD_ID = re.compile(r"MR-(0|[1-9][0-9]*)")
_PCD_ID = re.compile(r"PCD-(0|[1-9][0-9]*)")
_CANDIDATE_REF = re.compile(r"tree#(?P<component>[^/#\s]+)/node#(?P<node>[^/#\s]+)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SPINE_ID = re.compile(r"(?m)^id: (SP-L4-(?:0|[1-9][0-9]*))$")


class PacketError(RuntimeError):
    """Stable failure surface for packet preparation."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


def _packet_error(exc: ScreenInputError, *, operation: str) -> PacketError:
    detail = str(exc)
    path_markers = (
        "path", "anchor", "local name", "NFC", "case-fold", "alias", "missing exact"
    )
    kind = (
        "snapshot-path-failure"
        if any(marker in detail for marker in path_markers)
        else "snapshot-object-failure"
    )
    return PacketError(kind, f"{operation}: {detail}")


def _git_text(root: Path, args: list[str], label: str) -> str:
    try:
        raw = _run_git_binary(root, args)
    except ScreenInputError as exc:
        raise _packet_error(exc, operation=label) from exc
    try:
        return raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise PacketError(
            "snapshot-object-failure", f"{label}: Git returned invalid UTF-8"
        ) from exc


@dataclass(frozen=True)
class DecisionSnapshot:
    """One explicit physical commit/tree pair captured for a decision."""

    root: Path
    head_commit: str
    head_tree: str

    @classmethod
    def capture(cls, root: str | Path) -> "DecisionSnapshot":
        try:
            captured = GitSnapshot.capture(root)
        except ScreenInputError as exc:
            raise _packet_error(exc, operation="cannot capture decision snapshot") from exc
        return cls(captured.root, captured.head_commit, captured.head_tree)

    @classmethod
    def from_commit(cls, root: str | Path, commit: str) -> "DecisionSnapshot":
        resolved = Path(root).expanduser().resolve()
        if not isinstance(commit, str) or _OID.fullmatch(commit) is None:
            raise PacketError(
                "snapshot-failure", "decision snapshot commit must be a lowercase Git OID"
            )
        actual = _git_text(
            resolved,
            ["rev-parse", "--verify", f"{commit}^{{commit}}"],
            "cannot resolve decision commit",
        )
        if actual != commit:
            raise PacketError(
                "snapshot-failure",
                f"decision commit resolved to a different object: {actual}",
            )
        tree = _git_text(
            resolved,
            ["rev-parse", "--verify", f"{commit}^{{tree}}"],
            "cannot resolve decision tree",
        )
        if _OID.fullmatch(tree) is None:
            raise PacketError(
                "snapshot-object-failure", f"decision tree has invalid Git OID {tree!r}"
            )
        return cls(resolved, commit, tree)

    def validate_for(self, root: str | Path) -> Path:
        resolved = Path(root).expanduser().resolve()
        if resolved != self.root.expanduser().resolve():
            raise PacketError(
                "snapshot-failure",
                "decision snapshot root does not match the supplied research root",
            )
        if _OID.fullmatch(self.head_commit) is None or _OID.fullmatch(self.head_tree) is None:
            raise PacketError(
                "snapshot-failure", "decision snapshot requires lowercase commit and tree OIDs"
            )
        actual_commit = _git_text(
            resolved,
            ["rev-parse", "--verify", f"{self.head_commit}^{{commit}}"],
            "cannot validate decision commit",
        )
        actual_tree = _git_text(
            resolved,
            ["rev-parse", "--verify", f"{self.head_commit}^{{tree}}"],
            "cannot validate decision tree",
        )
        if actual_commit != self.head_commit:
            raise PacketError(
                "snapshot-failure",
                f"decision commit resolved to a different object: {actual_commit}",
            )
        if actual_tree != self.head_tree:
            raise PacketError(
                "snapshot-object-failure",
                "decision snapshot commit/tree pair does not match",
            )
        return resolved


@dataclass(frozen=True)
class SourceArtifact:
    source_ref: str
    git_oid: str
    content: bytes


class SnapshotReader:
    """Path-safe reader over one explicit physical committed tree object.

    Callers can read an exact blob, an optional blob, or a complete committed
    subtree.  All traversal retains the packet builder's strict mode, name,
    normalized-alias, and object checks.
    """

    def __init__(self, snapshot: DecisionSnapshot) -> None:
        git_snapshot = GitSnapshot(
            snapshot.root, snapshot.head_commit, snapshot.head_tree, "unavailable"
        )
        self._discovery = Discovery(git_snapshot)
        self._root_tree = snapshot.head_tree

    def _children(self, tree_oid: str, operation: str) -> tuple[GitEntry, ...]:
        try:
            return self._discovery._list_tree(tree_oid)
        except ScreenInputError as exc:
            raise _packet_error(exc, operation=operation) from exc

    def _segments(self, source_ref: str) -> tuple[str, ...]:
        if (
            not isinstance(source_ref, str)
            or not source_ref
            or source_ref.startswith("/")
            or "\\" in source_ref
        ):
            raise PacketError(
                "snapshot-path-failure", f"unsafe committed source ref {source_ref!r}"
            )
        parts = tuple(source_ref.split("/"))
        if any(not part or part in {".", ".."} for part in parts):
            raise PacketError(
                "snapshot-path-failure", f"unsafe committed source ref {source_ref!r}"
            )
        return parts

    def _entry(self, source_ref: str) -> GitEntry:
        parts = self._segments(source_ref)
        tree_oid = self._root_tree
        walked: list[str] = []
        for ordinal, part in enumerate(parts):
            children = self._children(tree_oid, f"cannot enumerate {'/'.join(walked) or '<root>'}")
            exact = next((entry for entry in children if entry.local_name == part), None)
            aliases = [
                entry.local_name
                for entry in children
                if entry.local_name != part
                and comparison_key(entry.local_name) == comparison_key(part)
            ]
            if aliases:
                raise PacketError(
                    "snapshot-path-failure",
                    f"normalized alias for committed source ref {source_ref}: {aliases!r}",
                )
            if exact is None:
                raise PacketError(
                    "snapshot-path-failure", f"missing committed source ref {source_ref}"
                )
            walked.append(part)
            if ordinal < len(parts) - 1:
                if exact.mode != "040000" or exact.object_type != "tree":
                    raise PacketError(
                        "snapshot-object-failure",
                        f"committed path {'/'.join(walked)} is not a tree",
                    )
                tree_oid = exact.oid
        return GitEntry(
            parts,
            parts[-1],
            exact.raw_name,
            exact.mode,
            exact.object_type,
            exact.oid,
        )

    def read(self, source_ref: str) -> SourceArtifact:
        entry = self._entry(source_ref)
        if entry.mode != "100644" or entry.object_type != "blob":
            raise PacketError(
                "snapshot-object-failure",
                f"committed source {source_ref} must be a 100644 blob, got "
                f"{entry.mode} {entry.object_type}",
            )
        try:
            content = self._discovery._blob_bytes(entry)
        except ScreenInputError as exc:
            raise _packet_error(exc, operation=f"cannot read {source_ref}") from exc
        return SourceArtifact(source_ref, entry.oid, content)

    def read_optional(self, source_ref: str) -> SourceArtifact | None:
        try:
            return self.read(source_ref)
        except PacketError as exc:
            if exc.kind == "snapshot-path-failure" and exc.message.startswith(
                "missing committed source ref"
            ):
                return None
            raise

    def files_under(self, source_ref: str) -> tuple[SourceArtifact, ...]:
        anchor = self._entry(source_ref)
        if anchor.mode != "040000" or anchor.object_type != "tree":
            raise PacketError(
                "snapshot-object-failure", f"committed store {source_ref} is not a tree"
            )
        work: list[tuple[tuple[str, ...], str]] = [
            (self._segments(source_ref), anchor.oid)
        ]
        files: list[SourceArtifact] = []
        while work:
            parent, tree_oid = work.pop()
            children = self._children(tree_oid, f"cannot enumerate {'/'.join(parent)}")
            descend: list[tuple[tuple[str, ...], str]] = []
            for child in children:
                path = (*parent, child.local_name)
                display = "/".join(path)
                if child.mode == "040000" and child.object_type == "tree":
                    descend.append((path, child.oid))
                elif child.mode == "100644" and child.object_type == "blob":
                    entry = GitEntry(
                        path,
                        child.local_name,
                        child.raw_name,
                        child.mode,
                        child.object_type,
                        child.oid,
                    )
                    try:
                        content = self._discovery._blob_bytes(entry)
                    except ScreenInputError as exc:
                        raise _packet_error(exc, operation=f"cannot read {display}") from exc
                    files.append(SourceArtifact(display, child.oid, content))
                else:
                    raise PacketError(
                        "snapshot-object-failure",
                        f"unsupported committed mode/type at {display}: "
                        f"{child.mode} {child.object_type}",
                    )
            work.extend(reversed(descend))
        return tuple(sorted(files, key=lambda item: stable_path_key(item.source_ref)))


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise PacketError("packet-invalid", f"cannot serialize canonical packet JSON: {exc}") from exc


def _json_artifact(artifact: SourceArtifact) -> dict[str, Any]:
    try:
        value = json.loads(artifact.content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PacketError(
            "snapshot-object-failure",
            f"committed JSON source {artifact.source_ref} is unreadable: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise PacketError(
            "snapshot-object-failure",
            f"committed JSON source {artifact.source_ref} must contain an object",
        )
    return value


def _numeric_id(value: Any, pattern: re.Pattern[str], label: str) -> int:
    if not isinstance(value, str) or (match := pattern.fullmatch(value)) is None:
        raise PacketError(f"{label}-order-invalid", f"invalid canonical {label} id {value!r}")
    return int(match.group(1))


def _record_sources(reader: SnapshotReader) -> list[tuple[int, SourceArtifact, dict]]:
    records: list[tuple[int, SourceArtifact, dict]] = []
    seen: dict[int, str] = {}
    prefix = "tier1/merge-records/"
    for source in reader.files_under("tier1/merge-records"):
        relative = source.source_ref.removeprefix(prefix)
        if relative == ".gitkeep":
            if source.content:
                raise PacketError(
                    "snapshot-object-failure", "tier1/merge-records/.gitkeep is not empty"
                )
            continue
        if "/" in relative or not relative.endswith(".json"):
            raise PacketError(
                "snapshot-path-failure",
                f"unexpected committed merge-record path {source.source_ref}",
            )
        path_id = relative[:-5]
        ordinal = _numeric_id(path_id, _RECORD_ID, "record")
        document = _json_artifact(source)
        if document.get("id") != path_id:
            raise PacketError(
                "record-order-invalid",
                f"merge-record id {document.get('id')!r} does not match {source.source_ref}",
            )
        consumed_epoch = document.get("consumed_epoch")
        if consumed_epoch is not None and (
            not isinstance(consumed_epoch, int)
            or isinstance(consumed_epoch, bool)
            or consumed_epoch < 0
        ):
            raise PacketError(
                "snapshot-object-failure",
                f"merge-record {path_id} has malformed consumed_epoch {consumed_epoch!r}",
            )
        if document.get("gate_verdict") is not None and not isinstance(
            document.get("gate_verdict"), dict
        ):
            raise PacketError(
                "snapshot-object-failure",
                f"merge-record {path_id} has malformed gate_verdict",
            )
        if not isinstance(document.get("scope"), dict):
            raise PacketError(
                "snapshot-object-failure", f"merge-record {path_id} has malformed scope"
            )
        if document.get("watch_link") is not None and not isinstance(
            document.get("watch_link"), str
        ):
            raise PacketError(
                "snapshot-object-failure",
                f"merge-record {path_id} has malformed watch_link",
            )
        if ordinal in seen:
            raise PacketError(
                "record-order-invalid",
                f"duplicate numeric merge-record id {ordinal}: {seen[ordinal]}, {source.source_ref}",
            )
        seen[ordinal] = source.source_ref
        records.append((ordinal, source, document))
    return sorted(records, key=lambda row: row[0])


def _tree_sources(reader: SnapshotReader) -> list[tuple[str, SourceArtifact, dict]]:
    files = reader.files_under("trees")
    tree_docs: list[tuple[str, SourceArtifact, dict]] = []
    component_files: dict[str, set[str]] = {}
    for source in files:
        parts = source.source_ref.split("/")
        if len(parts) < 2:
            continue
        component = parts[1]
        if component == ".gitkeep":
            if source.content:
                raise PacketError("snapshot-object-failure", "trees/.gitkeep is not empty")
            continue
        component_files.setdefault(component, set()).add(source.source_ref)
        folded = tuple(comparison_key(part) for part in parts)
        if len(parts) == 3 and folded[2] == comparison_key("tree.json"):
            if parts[2] != "tree.json":
                raise PacketError(
                    "snapshot-path-failure", f"tree state path is not exact: {source.source_ref}"
                )
            document = _json_artifact(source)
            if document.get("component") != component:
                raise PacketError(
                    "snapshot-object-failure",
                    f"tree component does not match committed path {source.source_ref}",
                )
            tree_docs.append((component, source, document))
    observed_tree_components = {component for component, _source, _doc in tree_docs}
    for component in component_files:
        if component not in observed_tree_components:
            raise PacketError(
                "snapshot-path-failure",
                f"committed tree directory trees/{component} is missing exact tree.json",
            )
    tree_docs.sort(key=lambda row: stable_path_key(row[1].source_ref))
    return tree_docs


def _candidate_join(
    reader: SnapshotReader,
    record_id: str,
    record: dict,
) -> tuple[SourceArtifact, dict, SourceArtifact, dict, SourceArtifact, list[dict]]:
    candidate_ref = record.get("candidate_ref")
    match = _CANDIDATE_REF.fullmatch(candidate_ref) if isinstance(candidate_ref, str) else None
    if match is None:
        raise PacketError(
            "report-join-missing",
            f"candidate-report join for {record_id} has invalid candidate_ref {candidate_ref!r}",
        )
    component = match.group("component")
    node_id = match.group("node")
    node_source = reader.read(f"trees/{component}/nodes/{node_id}/node.json")
    node = _json_artifact(node_source)
    if node.get("id") != node_id:
        raise PacketError(
            "report-join-missing",
            f"candidate-report join for {record_id} found a node id/path mismatch",
        )
    claims = node.get("claims")
    if not isinstance(claims, list):
        claims = []
    granted = [claim for claim in claims if isinstance(claim, dict) and claim.get("status") == "granted"]
    if any(claim.get("node") != node_id for claim in granted):
        raise PacketError(
            "report-join-missing",
            f"candidate-report join for {record_id} found a granted claim outside candidate node {node_id}",
        )
    source_ids = {
        claim.get("source_dispatch")
        for claim in granted
        if isinstance(claim.get("source_dispatch"), str) and claim.get("source_dispatch")
    }
    if not granted or not source_ids:
        raise PacketError(
            "report-join-missing",
            f"candidate-report join for {record_id} requires granted claims citing one source dispatch; found none",
        )
    if len(source_ids) != 1 or any(
        not isinstance(claim.get("source_dispatch"), str) or not claim.get("source_dispatch")
        for claim in granted
    ):
        raise PacketError(
            "report-join-ambiguous",
            f"candidate-report join for {record_id} has multiple or malformed granted-claim source dispatches: "
            f"{sorted(value for value in source_ids if isinstance(value, str))}",
        )
    source_dispatch_id = next(iter(source_ids))
    dispatch_dir = f"trees/{component}/nodes/{node_id}/dispatches"
    try:
        dispatch_files = reader.files_under(dispatch_dir)
    except PacketError as exc:
        if exc.kind == "snapshot-path-failure":
            raise PacketError(
                "report-join-missing",
                f"candidate-report join for {record_id} is missing {dispatch_dir}",
            ) from exc
        raise
    eligible: list[tuple[SourceArtifact, dict]] = []
    for source in dispatch_files:
        relative = source.source_ref.removeprefix(dispatch_dir + "/")
        if "/" in relative or not relative.endswith(".json"):
            raise PacketError(
                "snapshot-path-failure", f"unexpected committed dispatch path {source.source_ref}"
            )
        dispatch = _json_artifact(source)
        path_dispatch_id = relative[:-5]
        if dispatch.get("id") != path_dispatch_id or dispatch.get("node") != node_id:
            raise PacketError(
                "report-join-missing",
                f"candidate-report join for {record_id} found a dispatch id/node path mismatch at "
                f"{source.source_ref}",
            )
        if (
            dispatch.get("outcome") == "completed"
            and isinstance(dispatch.get("report_ref"), str)
            and dispatch.get("report_ref")
            and isinstance(dispatch.get("report_hash"), str)
            and dispatch.get("report_hash")
        ):
            eligible.append((source, dispatch))
    if not eligible:
        raise PacketError(
            "report-join-missing",
            f"candidate-report join for {record_id} has no completed dispatch with report_ref/report_hash",
        )
    if len(eligible) != 1:
        raise PacketError(
            "report-join-ambiguous",
            f"candidate-report join for {record_id} has {len(eligible)} completed dispatches with reports",
        )
    dispatch_source, dispatch = eligible[0]
    if dispatch.get("id") != source_dispatch_id:
        raise PacketError(
            "report-join-missing",
            f"candidate-report join for {record_id} requires source dispatch {source_dispatch_id!r} "
            f"but the unique completed report dispatch is {dispatch.get('id')!r}",
        )
    report_ref = dispatch["report_ref"]
    report_prefix = f"trees/{component}/nodes/{node_id}/reports/"
    if not report_ref.startswith(report_prefix) or "/" in report_ref.removeprefix(
        report_prefix
    ):
        raise PacketError(
            "report-join-missing",
            f"candidate-report join for {record_id} has cross-node or nested report_ref {report_ref!r}",
        )
    report_source = reader.read(report_ref)
    actual_hash = hashlib.sha256(report_source.content).hexdigest()
    expected_hash = dispatch["report_hash"]
    if _SHA256.fullmatch(expected_hash) is None or actual_hash != expected_hash:
        raise PacketError(
            "report-hash-mismatch",
            f"candidate-report join for {record_id} hash mismatch at {report_ref}",
        )
    return node_source, node, dispatch_source, dispatch, report_source, granted


def _pc_decisions(
    reader: SnapshotReader, record_id: str
) -> list[tuple[int, SourceArtifact, dict]]:
    decisions: list[tuple[int, SourceArtifact, dict]] = []
    seen: dict[int, str] = {}
    prefix = "tier1/decision-log/"
    for source in reader.files_under("tier1/decision-log"):
        relative = source.source_ref.removeprefix(prefix)
        if relative == ".gitkeep":
            if source.content:
                raise PacketError(
                    "snapshot-object-failure", "tier1/decision-log/.gitkeep is not empty"
                )
            continue
        if "/" in relative or not relative.endswith(".json"):
            raise PacketError(
                "pcd-order-invalid", f"cannot order committed PC decision path {source.source_ref}"
            )
        path_id = relative[:-5]
        ordinal = _numeric_id(path_id, _PCD_ID, "pcd")
        document = _json_artifact(source)
        if document.get("id") != path_id:
            raise PacketError(
                "pcd-order-invalid",
                f"PC decision id {document.get('id')!r} does not match {source.source_ref}",
            )
        if ordinal in seen:
            raise PacketError(
                "pcd-order-invalid",
                f"duplicate numeric PC decision id {ordinal}: {seen[ordinal]}, {source.source_ref}",
            )
        seen[ordinal] = source.source_ref
        if document.get("kind") != "merge-schedule":
            continue
        refs = document.get("context_refs")
        if not isinstance(refs, list):
            raise PacketError(
                "pcd-context-invalid",
                f"merge-schedule {path_id} context_refs must be a list",
            )
        seen_refs: set[str] = set()
        for ref in refs:
            if not isinstance(ref, str) or not ref or ref.strip() != ref:
                raise PacketError(
                    "pcd-context-invalid",
                    f"merge-schedule {path_id} has malformed context ref {ref!r}",
                )
            if ref in seen_refs:
                raise PacketError(
                    "pcd-context-invalid",
                    f"merge-schedule {path_id} repeats context ref {ref!r}",
                )
            seen_refs.add(ref)
            if ref.startswith("MR") and _RECORD_ID.fullmatch(ref) is None:
                raise PacketError(
                    "pcd-context-invalid",
                    f"merge-schedule {path_id} has malformed MR context ref {ref!r}",
                )
        if record_id in seen_refs:
            decisions.append((ordinal, source, document))
    return sorted(decisions, key=lambda row: row[0])


@dataclass(frozen=True)
class _ArtifactPlan:
    source: SourceArtifact
    packet_ref: str
    artifact_kind: str


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9.-]+", "-", value).strip("-.")
    return token or hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _artifact_plans(
    report: SourceArtifact,
    sources: Iterable[tuple[SourceArtifact, str, str]],
) -> list[_ArtifactPlan]:
    plans = [_ArtifactPlan(report, "packet/001-report.md", "candidate-report")]
    seen = {report.source_ref}
    ordinal = 2
    for source, kind, label in sources:
        if source.source_ref in seen:
            continue
        seen.add(source.source_ref)
        suffix = ".md" if source.source_ref.endswith(".md") else ".json"
        name = f"{ordinal:03d}-{_safe_token(label)}{suffix}"
        plans.append(_ArtifactPlan(source, f"packet/{name}", kind))
        ordinal += 1
    return plans


def allocate_attempt(
    research_root: str | Path,
    record_id: str,
    *,
    attempt_id: str | None = None,
) -> tuple[str, Path]:
    """Allocate one no-overwrite attempt directory below a caller-supplied root."""

    if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
        raise PacketError("packet-invalid", f"record id must be canonical MR-N, got {record_id!r}")
    chosen = secrets.token_hex(16) if attempt_id is None else attempt_id
    if not isinstance(chosen, str) or _ATTEMPT_ID.fullmatch(chosen) is None:
        raise PacketError(
            "invalid-attempt-id", "attempt id must be exactly 32 lowercase hexadecimal characters"
        )
    root = Path(research_root).expanduser().resolve()
    if not root.is_dir():
        raise PacketError("write-collision", f"supplied research root is not a directory: {root}")
    attempts = root / "var" / "cgate" / record_id / "attempts"
    for parent in (
        root / "var",
        root / "var" / "cgate",
        root / "var" / "cgate" / record_id,
        attempts,
    ):
        if parent.is_symlink():
            raise PacketError(
                "write-collision", f"attempt output parent is unsafe: {parent}"
            )
        try:
            os.mkdir(parent, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PacketError(
                "write-collision", f"cannot create attempt parent {parent}: {exc}"
            ) from exc
        if parent.is_symlink() or not parent.is_dir() or not parent.resolve().is_relative_to(root):
            raise PacketError(
                "write-collision", f"attempt output parent is unsafe: {parent}"
            )
    attempt_dir = attempts / chosen
    try:
        os.mkdir(attempt_dir, 0o700)
    except FileExistsError as exc:
        raise PacketError(
            "attempt-collision", f"attempt id {chosen} already exists for {record_id}"
        ) from exc
    except OSError as exc:
        raise PacketError(
            "write-collision", f"cannot allocate attempt id {chosen} for {record_id}: {exc}"
        ) from exc
    return chosen, attempt_dir


def _exclusive_write(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise PacketError("write-collision", f"refusing to overwrite packet file {path}") from exc
    except OSError as exc:
        raise PacketError("write-collision", f"cannot write packet file {path}: {exc}") from exc


@dataclass(frozen=True)
class PreparedPacket:
    attempt_id: str
    attempt_dir: Path
    packet_dir: Path
    manifest_ref: str
    manifest_sha256: str
    artifact_refs: tuple[str, ...]
    input_hashes: dict[str, str]


def prepare_packet(
    research_root: str | Path,
    record_id: str,
    *,
    snapshot: DecisionSnapshot,
    attempt_id: str | None = None,
    allocated_attempt: tuple[str, Path] | None = None,
) -> PreparedPacket:
    """Prepare one immutable stage-2 packet from exactly ``snapshot``."""

    root = snapshot.validate_for(research_root)
    snapshot = DecisionSnapshot(root, snapshot.head_commit, snapshot.head_tree)
    if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
        raise PacketError("packet-invalid", f"record id must be canonical MR-N, got {record_id!r}")
    if allocated_attempt is None:
        chosen_attempt, attempt_dir = allocate_attempt(
            root, record_id, attempt_id=attempt_id
        )
    else:
        chosen_attempt, attempt_dir = allocated_attempt
        expected = root / "var" / "cgate" / record_id / "attempts" / chosen_attempt
        if (
            attempt_id is not None
            or _ATTEMPT_ID.fullmatch(chosen_attempt) is None
            or attempt_dir != expected
            or attempt_dir.is_symlink()
            or not attempt_dir.is_dir()
            or any(attempt_dir.iterdir())
        ):
            raise PacketError(
                "attempt-identity-invalid",
                "preallocated attempt identity/path is unsafe or not empty",
            )
    reader = SnapshotReader(snapshot)
    records = _record_sources(reader)
    matches = [row for row in records if row[2].get("id") == record_id]
    if not matches:
        raise PacketError("snapshot-path-failure", f"missing committed merge record {record_id}")
    if len(matches) != 1:
        raise PacketError("record-order-invalid", f"duplicate committed merge record {record_id}")
    _record_number, record_source, record = matches[0]
    screen = record.get("screen")
    if (
        not isinstance(screen, dict)
        or not isinstance(screen.get("results"), list)
        or not screen.get("results")
        or any(screen.get(field) is None for field in ("output_ref", "log_ref", "computed", "config_hash", "engine_version"))
    ):
        raise PacketError(
            "packet-invalid", f"merge record {record_id} lacks a completed stage-1 transcription"
        )
    scope = record.get("scope")
    surfaces = scope.get("surfaces") if isinstance(scope, dict) else None
    if not isinstance(surfaces, list) or any(not isinstance(item, str) for item in surfaces):
        raise PacketError("packet-invalid", f"merge record {record_id} has malformed scope.surfaces")

    node_source, node, dispatch_source, dispatch, report_source, granted = _candidate_join(
        reader, record_id, record
    )
    tree_docs = _tree_sources(reader)

    watch_rows: dict[str, list[dict]] = {}
    for component, _tree_source, tree_document in tree_docs:
        queue = tree_document.get("watch_queue")
        if not isinstance(queue, list) or any(not isinstance(row, dict) for row in queue):
            raise PacketError(
                "snapshot-object-failure", f"tree {component} has malformed watch_queue"
            )
        for row in queue:
            row_id = row.get("id")
            if isinstance(row_id, str) and row_id:
                watch_rows.setdefault(row_id, []).append(row)

    def watch_outcome(link: Any) -> Any:
        if link is None:
            return None
        linked = watch_rows.get(link, [])
        if len(linked) > 1:
            raise PacketError(
                "snapshot-object-failure", f"watch link {link!r} is ambiguous across trees"
            )
        return linked[0].get("verdict") if linked else None

    consumed = [row for row in records if isinstance(row[2].get("consumed_epoch"), int)]
    # Newest epochs first; equal-epoch records retain ascending numeric MR order.
    consumed.sort(key=lambda row: (-row[2]["consumed_epoch"], row[0]))
    consumed = consumed[:5]
    pending = [
        row
        for row in records
        if row[2].get("consumed_epoch") is None
        and (
            row[2].get("gate_verdict") is None
            or (
                isinstance(row[2].get("gate_verdict"), dict)
                and row[2]["gate_verdict"].get("verdict") == "land"
            )
        )
    ]
    # The pending frontier is a numeric MR sequence, independent of path collation.
    pending.sort(key=lambda row: row[0])

    # W2 defines cross-lane settlement completeness over queued staleness rows,
    # not over the separate node-conflict settlement lifecycle.
    open_settlements: list[tuple[SourceArtifact, str, str, int, str, dict]] = []
    for component, source, tree_document in tree_docs:
        tree_path = source.source_ref
        for row in tree_document["watch_queue"]:
            if row.get("kind") != "staleness-assessment" or row.get("status") != "queued":
                continue
            row_id = row.get("id")
            epoch = row.get("epoch")
            merged_node = row.get("merged_node")
            if not isinstance(row_id, str) or not row_id:
                raise PacketError(
                    "snapshot-object-failure",
                    f"queued watch row in {tree_path} needs a non-empty id",
                )
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
                raise PacketError(
                    "snapshot-object-failure",
                    f"queued watch row {row_id} in {tree_path} needs an epoch",
                )
            if not isinstance(merged_node, str) or not merged_node:
                raise PacketError(
                    "snapshot-object-failure",
                    f"queued watch row {row_id} in {tree_path} needs merged_node",
                )
            open_settlements.append(
                (source, component, row_id, epoch, merged_node, row)
            )
    open_settlements.sort(
        key=lambda item: (
            stable_path_key(item[0].source_ref),
            stable_text_key(item[2]),
        )
    )

    pc_decisions = _pc_decisions(reader, record_id)
    operative_pcd = pc_decisions[-1][2]["id"] if pc_decisions else None

    spine_source: SourceArtifact | None = None
    spine_ids: list[str] = []
    # The composition gate is an elevated cross-lane seat.  Frozen section 7
    # and CG-P3 call for relevant spine entries without candidate-lane scoping,
    # so the committed L4 catalogue is included for every candidate when present.
    spine_source = reader.read_optional("system/observatory/spine-candidates.L4.v1.md")
    if spine_source is not None:
        try:
            spine_text = spine_source.content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PacketError(
                "snapshot-object-failure", "committed L4 spine candidates are not UTF-8"
            ) from exc
        seen_spine: set[int] = set()
        for value in _SPINE_ID.findall(spine_text):
            ordinal = int(value.rsplit("-", 1)[1])
            if ordinal in seen_spine:
                raise PacketError(
                    "snapshot-object-failure", f"duplicate L4 spine entry id {value}"
                )
            seen_spine.add(ordinal)
            spine_ids.append(value)
        spine_ids.sort(key=lambda value: int(value.rsplit("-", 1)[1]))

    source_rows: list[tuple[SourceArtifact, str, str]] = [
        (record_source, "merge-record", f"merge-record-{record_id}"),
        (node_source, "candidate-node", "candidate-node"),
        (dispatch_source, "source-dispatch", "source-dispatch"),
    ]
    source_rows.extend(
        (source, "consumed-merge-record", f"consumed-{document['id']}")
        for _number, source, document in consumed
    )
    source_rows.extend(
        (source, "pending-merge-record", f"pending-{document['id']}")
        for _number, source, document in pending
    )
    source_rows.extend(
        (source, "watch-tree", f"watch-tree-{component}")
        for component, source, _document in tree_docs
    )
    source_rows.extend(
        (source, "pc-decision", f"pc-decision-{document['id']}")
        for _number, source, document in pc_decisions
    )
    if spine_source is not None:
        source_rows.append((spine_source, "l4-spine", "l4-spine-candidates"))
    plans = _artifact_plans(report_source, source_rows)
    by_source = {plan.source.source_ref: plan for plan in plans}

    context = {
        "format": PACKET_FORMAT,
        "snapshot": {
            "head_commit": snapshot.head_commit,
            "head_tree": snapshot.head_tree,
        },
        "merge_record": record,
        "merge_record_source": {
            "source_ref": record_source.source_ref,
            "packet_ref": by_source[record_source.source_ref].packet_ref,
        },
        "candidate": {
            "candidate_ref": record["candidate_ref"],
            "node": node,
            "node_source": {
                "source_ref": node_source.source_ref,
                "packet_ref": by_source[node_source.source_ref].packet_ref,
            },
            "granted_claims": granted,
            "source_dispatch": dispatch,
            "source_dispatch_ref": {
                "source_ref": dispatch_source.source_ref,
                "packet_ref": by_source[dispatch_source.source_ref].packet_ref,
            },
            "report": {
                "source_ref": report_source.source_ref,
                "packet_ref": "packet/001-report.md",
                "sha256": hashlib.sha256(report_source.content).hexdigest(),
            },
        },
        "effective_surfaces": {
            "before": None,
            "after": surfaces,
            "before_status": "unavailable-in-v1",
        },
        "last_consumed_merge_records": [
            {
                "record": document,
                "source_ref": source.source_ref,
                "packet_ref": by_source[source.source_ref].packet_ref,
                "watch_link": document.get("watch_link"),
                "watch_outcome": watch_outcome(document.get("watch_link")),
            }
            for _number, source, document in consumed
        ],
        "open_settlements": [
            {
                "tree_path": source.source_ref,
                "component": component,
                "row_id": row_id,
                "epoch": epoch,
                "merged_node": merged_node,
                "row": row,
                "source_ref": source.source_ref,
                "packet_ref": by_source[source.source_ref].packet_ref,
            }
            for source, component, row_id, epoch, merged_node, row in open_settlements
        ],
        "pending_merge_records": [
            {
                "id": document["id"],
                "candidate_ref": document.get("candidate_ref"),
                "scope": document.get("scope"),
                "source_ref": source.source_ref,
                "packet_ref": by_source[source.source_ref].packet_ref,
            }
            for _number, source, document in pending
        ],
        "merge_schedule_pc_decisions": [
            {
                "id": document["id"],
                "operative": document["id"] == operative_pcd,
                "decision": document,
                "source_ref": source.source_ref,
                "packet_ref": by_source[source.source_ref].packet_ref,
            }
            for _number, source, document in pc_decisions
        ],
        "operative_merge_schedule_pc_decision": operative_pcd,
        "l4_spine_entries": [
            {
                "id": spine_id,
                "label": "provisional pending RQ-7",
                "source_ref": spine_source.source_ref,
                "packet_ref": by_source[spine_source.source_ref].packet_ref,
            }
            for spine_id in spine_ids
        ] if spine_source is not None else [],
    }
    context_bytes = _canonical_bytes(context)

    packet_dir = attempt_dir / "packet"
    try:
        os.mkdir(packet_dir, 0o700)
    except FileExistsError as exc:
        raise PacketError("write-collision", f"packet directory already exists: {packet_dir}") from exc
    except OSError as exc:
        raise PacketError("write-collision", f"cannot create packet directory {packet_dir}: {exc}") from exc

    generated_ref = "packet/context.json"
    all_entries: list[dict[str, Any]] = [
        {
            "source_ref": "generated:packet-context",
            "git_oid": None,
            "packet_ref": generated_ref,
            "artifact_kind": "packet-context",
            "sha256": hashlib.sha256(context_bytes).hexdigest(),
        }
    ]
    for plan in plans:
        all_entries.append(
            {
                "source_ref": plan.source.source_ref,
                "git_oid": plan.source.git_oid,
                "packet_ref": plan.packet_ref,
                "artifact_kind": plan.artifact_kind,
                "sha256": hashlib.sha256(plan.source.content).hexdigest(),
            }
        )
    artifact_refs = [entry["packet_ref"] for entry in all_entries]
    input_hashes = {entry["packet_ref"]: entry["sha256"] for entry in all_entries}
    source_refs = {entry["packet_ref"]: entry["source_ref"] for entry in all_entries}
    manifest = {
        "format": MANIFEST_FORMAT,
        "attempt_id": chosen_attempt,
        "record_id": record_id,
        "snapshot": {
            "head_commit": snapshot.head_commit,
            "head_tree": snapshot.head_tree,
        },
        "artifacts": all_entries,
        "artifact_refs": artifact_refs,
        "input_hashes": input_hashes,
        "source_refs": source_refs,
    }
    manifest_bytes = _canonical_bytes(manifest)

    _exclusive_write(packet_dir / "context.json", context_bytes)
    for plan in plans:
        _exclusive_write(attempt_dir / plan.packet_ref, plan.source.content)
    _exclusive_write(packet_dir / "manifest.json", manifest_bytes)

    manifest_ref = (
        f"var/cgate/{record_id}/attempts/{chosen_attempt}/packet/manifest.json"
    )
    return PreparedPacket(
        attempt_id=chosen_attempt,
        attempt_dir=attempt_dir,
        packet_dir=packet_dir,
        manifest_ref=manifest_ref,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artifact_refs=tuple(artifact_refs),
        input_hashes=input_hashes,
    )


def prepare_failure_packet(
    research_root: str | Path,
    record_id: str,
    *,
    snapshot: DecisionSnapshot,
    allocated_attempt: tuple[str, Path],
    error: PacketError,
) -> PreparedPacket:
    """Write a minimal truthful packet for a pre-generator packet failure."""

    root = snapshot.validate_for(research_root)
    attempt_id, attempt_dir = allocated_attempt
    expected = root / "var" / "cgate" / record_id / "attempts" / attempt_id
    if (
        _ATTEMPT_ID.fullmatch(attempt_id) is None
        or attempt_dir != expected
        or attempt_dir.is_symlink()
        or not attempt_dir.is_dir()
        or any(attempt_dir.iterdir())
    ):
        raise PacketError(
            "attempt-identity-invalid",
            "cannot record packet failure in an unsafe or non-empty attempt",
        )
    context = {
        "format": PACKET_FORMAT,
        "snapshot": {
            "head_commit": snapshot.head_commit,
            "head_tree": snapshot.head_tree,
        },
        "record_id": record_id,
        "packet_status": "technical-failure",
        "error": {"kind": error.kind, "message": error.message},
    }
    context_bytes = _canonical_bytes(context)
    context_ref = "packet/context.json"
    context_hash = hashlib.sha256(context_bytes).hexdigest()
    artifacts = [
        {
            "source_ref": "generated:packet-preparation-error",
            "git_oid": None,
            "packet_ref": context_ref,
            "artifact_kind": "packet-context",
            "sha256": context_hash,
        }
    ]
    manifest = {
        "format": MANIFEST_FORMAT,
        "attempt_id": attempt_id,
        "record_id": record_id,
        "snapshot": {
            "head_commit": snapshot.head_commit,
            "head_tree": snapshot.head_tree,
        },
        "artifacts": artifacts,
        "artifact_refs": [context_ref],
        "input_hashes": {context_ref: context_hash},
        "source_refs": {
            context_ref: "generated:packet-preparation-error",
        },
    }
    manifest_bytes = _canonical_bytes(manifest)
    packet_dir = attempt_dir / "packet"
    try:
        os.mkdir(packet_dir, 0o700)
    except OSError as exc:
        raise PacketError(
            "write-collision", "cannot create packet-failure evidence directory"
        ) from exc
    _exclusive_write(packet_dir / "context.json", context_bytes)
    _exclusive_write(packet_dir / "manifest.json", manifest_bytes)
    return PreparedPacket(
        attempt_id=attempt_id,
        attempt_dir=attempt_dir,
        packet_dir=packet_dir,
        manifest_ref=(
            f"var/cgate/{record_id}/attempts/{attempt_id}/packet/manifest.json"
        ),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artifact_refs=(context_ref,),
        input_hashes={context_ref: context_hash},
    )


def review_prompt_bytes() -> bytes:
    """Load the frozen prompt from source checkout or either installed wheel."""

    package_path = Path(__file__).with_name("prompts") / PROMPT_NAME
    source_path = Path(__file__).resolve().parent.parent / "prompts" / PROMPT_NAME
    for candidate in (package_path, source_path):
        try:
            content = candidate.read_bytes()
        except FileNotFoundError:
            continue
        actual = hashlib.sha256(content).hexdigest()
        if actual != PROMPT_SHA256:
            raise PacketError(
                "prompt-hash-mismatch",
                f"packaged prompt {PROMPT_NAME} hash mismatch: expected "
                f"{PROMPT_SHA256}, got {actual}",
            )
        return content
    raise PacketError("snapshot-path-failure", f"packaged prompt {PROMPT_NAME} is missing")


__all__ = [
    "DecisionSnapshot",
    "MANIFEST_FORMAT",
    "PACKET_FORMAT",
    "PROMPT_NAME",
    "PROMPT_SHA256",
    "PacketError",
    "PreparedPacket",
    "SnapshotReader",
    "SourceArtifact",
    "allocate_attempt",
    "prepare_failure_packet",
    "prepare_packet",
    "review_prompt_bytes",
]
