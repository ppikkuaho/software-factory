"""Committed-Git-object discovery for the stage-1 composition screen.

The discovery boundary is one captured physical commit.  It walks tree objects
iteratively and non-recursively, records every immediate entry before descent,
and fails closed before any consumer can interpret a partial inventory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from types import MappingProxyType
from typing import Callable, Mapping, TypeVar
import unicodedata

from .normalization import comparison_key, stable_raw_name_key


GIT_TIMEOUT_SECONDS = 30.0
MERGE_RECORD_STORE = "tier1/merge-records"
TREE_STORE = "trees"
STORE_NAMES = (MERGE_RECORD_STORE, TREE_STORE)

_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_RECORD_NAME = re.compile(r"(?P<id>MR-[0-9]+)\.json")

# Git's environment API can redirect repository discovery, substitute object
# databases or indexes, inject configuration, and lazily fetch a missing
# promisor object.  Committed-object reads must inherit none of those knobs.
# Capture only host values required to execute a local binary and decode its
# output, then pin the read-side security settings.  This immutable template
# plus per-call copies avoids process-global mutation and cross-thread races.
_SAFE_HOST_ENV_NAMES = frozenset(
    {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SYSTEMROOT"}
)
_GIT_READ_ENV = MappingProxyType(
    {
        **{
            key: value
            for key, value in os.environ.items()
            if key in _SAFE_HOST_ENV_NAMES
        },
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
)


def git_read_environment() -> dict[str, str]:
    """Return the one sanitized environment for committed-object Git reads."""

    return dict(_GIT_READ_ENV)


@dataclass(frozen=True)
class GitEntry:
    """One immediate entry returned by a physical Git tree object."""

    path: tuple[str, ...]
    local_name: str
    raw_name: bytes
    mode: str
    object_type: str
    oid: str

    @property
    def display_path(self) -> str:
        return "/".join(self.path)

    def observed(self) -> dict[str, str]:
        return {"mode": self.mode, "type": self.object_type, "oid": self.oid}


class ScreenInputError(RuntimeError):
    """A committed input cannot be safely and completely interpreted."""

    def __init__(
        self,
        message: str,
        *,
        observed: GitEntry | None = None,
        tree_oid: str | None = None,
    ) -> None:
        super().__init__(message)
        self.observed = observed
        self.tree_oid = tree_oid


def _error_text(stdout: bytes, stderr: bytes) -> str:
    raw = stderr.strip() or stdout.strip()
    return raw.decode("utf-8", errors="replace") if raw else "unknown Git error"


def _run_git_binary(root: Path, args: list[str]) -> bytes:
    """Run one local Git command with the ruled timeout and no partial output."""

    resolved = root.expanduser().resolve()
    command = ["git", "--no-replace-objects", "-C", str(resolved), *args]
    try:
        process = subprocess.Popen(
            command,
            cwd=resolved,
            env=git_read_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ScreenInputError(f"cannot execute git command: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        process.wait()
        raise ScreenInputError(
            f"git command timed out after {GIT_TIMEOUT_SECONDS:.1f}s"
        ) from exc
    if process.returncode != 0:
        raise ScreenInputError(_error_text(stdout, stderr))
    return stdout


def _run_git_text(root: Path, args: list[str], *, label: str) -> str:
    raw = _run_git_binary(root, args)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScreenInputError(f"invalid UTF-8 from git while reading {label}") from exc


def _require_oid(value: str, label: str) -> str:
    oid = value.strip()
    if _OID.fullmatch(oid) is None:
        raise ScreenInputError(f"git returned invalid object id for {label}: {oid!r}")
    return oid


@dataclass(frozen=True)
class GitSnapshot:
    """The immutable physical commit used by one complete screen run."""

    root: Path
    head_commit: str
    head_tree: str
    computed: str

    @classmethod
    def capture(cls, root: str | Path) -> "GitSnapshot":
        resolved = Path(root).expanduser().resolve()
        commit = _require_oid(
            _run_git_text(
                resolved,
                ["rev-parse", "--verify", "HEAD^{commit}"],
                label="HEAD commit",
            ),
            "HEAD commit",
        )
        tree = _require_oid(
            _run_git_text(
                resolved,
                ["rev-parse", "--verify", f"{commit}^{{tree}}"],
                label="HEAD tree",
            ),
            "HEAD tree",
        )
        computed = _run_git_text(
            resolved,
            ["show", "-s", "--format=%cI", commit],
            label="commit timestamp",
        ).strip()
        if not computed:
            raise ScreenInputError("cannot read committed HEAD timestamp")
        return cls(resolved, commit, tree, computed)


@dataclass(frozen=True)
class StoreInventory:
    """A complete immutable inventory below one selected store anchor."""

    name: str
    root_entry: GitEntry
    entries: tuple[GitEntry, ...]
    by_path: Mapping[tuple[str, ...], GitEntry]


@dataclass(frozen=True)
class CommittedDocument:
    path: str
    entry: GitEntry
    value: dict


@dataclass(frozen=True)
class ClassifiedStore:
    inventory: StoreInventory
    documents: tuple[CommittedDocument, ...]


_T = TypeVar("_T")


class Discovery:
    """Value-or-error cached discovery and classifiers for one snapshot."""

    def __init__(self, snapshot: GitSnapshot):
        self.snapshot = snapshot
        self._tree_cache: dict[str, tuple[GitEntry, ...]] = {}
        self._store_cache: dict[str, ClassifiedStore | ScreenInputError] = {}

    @classmethod
    def capture(cls, root: str | Path) -> "Discovery":
        return cls(GitSnapshot.capture(root))

    def _list_tree(self, tree_oid: str) -> tuple[GitEntry, ...]:
        cached = self._tree_cache.get(tree_oid)
        if cached is not None:
            return cached
        raw = _run_git_binary(
            self.snapshot.root,
            [
                "ls-tree",
                "-z",
                "--full-tree",
                tree_oid,
            ],
        )
        if raw and not raw.endswith(b"\0"):
            raise ScreenInputError(
                f"malformed ls-tree output for {tree_oid}: incomplete NUL record"
            )
        records = raw[:-1].split(b"\0") if raw else []

        candidates: list[tuple[GitEntry, str]] = []
        for record in records:
            metadata, separator, name_raw = record.partition(b"\t")
            fields = metadata.split(b" ")
            if not separator or len(fields) != 3 or any(not field for field in fields):
                raise ScreenInputError(
                    f"malformed ls-tree output for {tree_oid}: expected "
                    "mode SP type SP oid TAB raw-name"
                )
            mode_raw, type_raw, oid_raw = fields
            try:
                mode = mode_raw.decode("ascii", errors="strict")
                object_type = type_raw.decode("ascii", errors="strict")
                oid = oid_raw.decode("ascii", errors="strict")
            except UnicodeDecodeError as exc:
                raise ScreenInputError(
                    f"invalid ASCII metadata in tree object {tree_oid}"
                ) from exc
            if _OID.fullmatch(oid) is None:
                raise ScreenInputError(
                    f"git returned invalid object id for entry in {tree_oid}: {oid!r}"
                )
            try:
                name = name_raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ScreenInputError(
                    f"invalid UTF-8 local name in tree object {tree_oid}"
                ) from exc
            if not name or "/" in name or name in {".", ".."}:
                raise ScreenInputError(
                    f"unsafe local name {name!r} in tree object {tree_oid}"
                )
            key = comparison_key(name)
            if key == ".git":
                raise ScreenInputError(
                    f"unsafe normalized .git local name {name!r} in tree object {tree_oid}"
                )
            candidates.append(
                (GitEntry((), name, name_raw, mode, object_type, oid), key)
            )

        # Duplicate identity is a parent-wide structural property.  Complete
        # that pass before canonical-form or object validation so an earlier
        # NFD/mode/type sibling cannot mask a later comparison-key twin.
        seen: dict[str, GitEntry] = {}
        for entry, key in candidates:
            previous = seen.get(key)
            if previous is not None:
                raise ScreenInputError(
                    "NFC+case-fold duplicate committed paths in tree "
                    f"{tree_oid}: {previous.local_name!r}, {entry.local_name!r}"
                )
            seen[key] = entry

        parsed: list[GitEntry] = []
        for entry, _key in candidates:
            if unicodedata.normalize("NFC", entry.local_name) != entry.local_name:
                raise ScreenInputError(
                    "non-NFC committed path local name "
                    f"{entry.local_name!r} in tree object {tree_oid}"
                )

            actual_type = _run_git_text(
                self.snapshot.root,
                ["cat-file", "-t", entry.oid],
                label=f"object {entry.oid}",
            ).strip()
            if actual_type != entry.object_type:
                raise ScreenInputError(
                    f"tree entry {entry.local_name!r} reports type "
                    f"{entry.object_type!r} but object {entry.oid} is {actual_type!r}"
                )
            mode_type = {
                "040000": "tree",
                "100644": "blob",
                "100755": "blob",
                "120000": "blob",
                "160000": "commit",
            }.get(entry.mode)
            if mode_type is not None and mode_type != entry.object_type:
                raise ScreenInputError(
                    f"mode/type mismatch for {entry.local_name!r}: "
                    f"{entry.mode} {entry.object_type}"
                )
            parsed.append(entry)

        result = tuple(
            sorted(parsed, key=lambda entry: stable_raw_name_key(entry.local_name, entry.raw_name))
        )
        self._tree_cache[tree_oid] = result
        return result

    def _anchor(
        self,
        parent_oid: str,
        parent_path: tuple[str, ...],
        exact_name: str,
        *,
        store_tree_oid: str | None = None,
    ) -> GitEntry:
        entries = self._list_tree(parent_oid)
        exact = next((entry for entry in entries if entry.local_name == exact_name), None)
        aliases = [
            entry.local_name
            for entry in entries
            if entry.local_name != exact_name
            and comparison_key(entry.local_name) == comparison_key(exact_name)
        ]
        display = "/".join((*parent_path, exact_name))
        if aliases:
            raise ScreenInputError(
                f"normalized alias for required committed anchor {display}: "
                + ", ".join(repr(value) for value in aliases),
                tree_oid=store_tree_oid,
            )
        if exact is None:
            raise ScreenInputError(
                f"missing exact committed anchor {display}", tree_oid=store_tree_oid
            )
        observed = GitEntry(
            (*parent_path, exact.local_name),
            exact.local_name,
            exact.raw_name,
            exact.mode,
            exact.object_type,
            exact.oid,
        )
        if exact.mode != "040000" or exact.object_type != "tree":
            raise ScreenInputError(
                f"required committed anchor {display} must be 040000 tree, got "
                f"{exact.mode} {exact.object_type}",
                observed=observed,
                tree_oid=store_tree_oid,
            )
        return observed

    def _store_anchor(self, name: str) -> GitEntry:
        if name == TREE_STORE:
            return self._anchor(self.snapshot.head_tree, (), "trees")
        if name == MERGE_RECORD_STORE:
            tier1 = self._anchor(self.snapshot.head_tree, (), "tier1")
            return self._anchor(tier1.oid, ("tier1",), "merge-records")
        raise AssertionError(f"unknown store {name}")

    def _walk_store(self, name: str) -> StoreInventory:
        anchor = self._store_anchor(name)
        worklist: list[tuple[tuple[str, ...], str]] = [(anchor.path, anchor.oid)]
        recorded: list[GitEntry] = []
        while worklist:
            parent_path, tree_oid = worklist.pop()
            try:
                children = self._list_tree(tree_oid)
            except ScreenInputError as exc:
                if exc.tree_oid is None:
                    exc.tree_oid = anchor.oid
                raise
            descend: list[tuple[tuple[str, ...], str]] = []
            for child in children:
                entry = GitEntry(
                    (*parent_path, child.local_name),
                    child.local_name,
                    child.raw_name,
                    child.mode,
                    child.object_type,
                    child.oid,
                )
                recorded.append(entry)
                if entry.mode == "040000" and entry.object_type == "tree":
                    descend.append((entry.path, entry.oid))
                elif entry.mode == "100644" and entry.object_type == "blob":
                    continue
                else:
                    raise ScreenInputError(
                        f"unsupported committed state mode/type at {entry.display_path}: "
                        f"{entry.mode} {entry.object_type}",
                        observed=entry,
                        tree_oid=anchor.oid,
                    )
            # Reverse a stable child order so the iterative LIFO walk visits it forward.
            worklist.extend(reversed(descend))
        ordered = tuple(
            sorted(
                recorded,
                key=lambda entry: tuple(
                    (comparison_key(part), part.encode("utf-8")) for part in entry.path
                ),
            )
        )
        return StoreInventory(
            name,
            anchor,
            ordered,
            MappingProxyType({entry.path: entry for entry in ordered}),
        )

    def _blob_bytes(self, entry: GitEntry) -> bytes:
        if entry.mode != "100644" or entry.object_type != "blob":
            raise ScreenInputError(
                f"committed document {entry.display_path} must be 100644 blob, got "
                f"{entry.mode} {entry.object_type}",
                observed=entry,
            )
        return _run_git_binary(self.snapshot.root, ["cat-file", "blob", entry.oid])

    def _empty_marker(self, entry: GitEntry, store_oid: str) -> None:
        try:
            size = len(self._blob_bytes(entry))
        except ScreenInputError as exc:
            if exc.tree_oid is None:
                exc.tree_oid = store_oid
            raise
        if size != 0:
            raise ScreenInputError(
                f"sanctioned committed marker {entry.display_path} must be empty bytes, got {size}",
                observed=entry,
                tree_oid=store_oid,
            )

    def _json_document(self, entry: GitEntry, store_oid: str) -> dict:
        raw = self._blob_bytes(entry)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ScreenInputError(
                f"unreadable JSON at {entry.display_path}: invalid UTF-8",
                observed=entry,
                tree_oid=store_oid,
            ) from exc
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScreenInputError(
                f"unreadable JSON at {entry.display_path}: {exc}",
                observed=entry,
                tree_oid=store_oid,
            ) from exc
        if not isinstance(value, dict):
            raise ScreenInputError(
                f"committed input {entry.display_path} must be a JSON object",
                observed=entry,
                tree_oid=store_oid,
            )
        return value

    def _classify_merge_records(self) -> ClassifiedStore:
        inventory = self._walk_store(MERGE_RECORD_STORE)
        root_len = len(inventory.root_entry.path)
        direct = [entry for entry in inventory.entries if len(entry.path) == root_len + 1]
        marker = next((entry for entry in direct if entry.local_name == ".gitkeep"), None)
        if marker is not None:
            self._empty_marker(marker, inventory.root_entry.oid)
        invalid = [
            entry.display_path
            for entry in inventory.entries
            if not (
                len(entry.path) == root_len + 1
                and (
                    entry.local_name == ".gitkeep"
                    or (
                        _RECORD_NAME.fullmatch(entry.local_name) is not None
                        and entry.mode == "100644"
                        and entry.object_type == "blob"
                    )
                )
            )
        ]
        if invalid:
            raise ScreenInputError(
                "unexpected committed merge-record path(s): " + ", ".join(invalid),
                tree_oid=inventory.root_entry.oid,
            )
        documents: list[CommittedDocument] = []
        for entry in direct:
            match = _RECORD_NAME.fullmatch(entry.local_name)
            if match is None:
                continue
            value = self._json_document(entry, inventory.root_entry.oid)
            record_id = value.get("id")
            if not isinstance(record_id, str) or not record_id:
                raise ScreenInputError(
                    f"field id in {entry.display_path} must be a non-empty string",
                    observed=entry,
                    tree_oid=inventory.root_entry.oid,
                )
            if record_id != match.group("id"):
                raise ScreenInputError(
                    f"merge-record id {record_id!r} does not match committed path "
                    f"{entry.display_path}",
                    observed=entry,
                    tree_oid=inventory.root_entry.oid,
                )
            documents.append(CommittedDocument(entry.display_path, entry, value))
        return ClassifiedStore(inventory, tuple(documents))

    def _classify_trees(self) -> ClassifiedStore:
        inventory = self._walk_store(TREE_STORE)
        root_len = len(inventory.root_entry.path)
        direct = [entry for entry in inventory.entries if len(entry.path) == root_len + 1]
        marker = next((entry for entry in direct if entry.local_name == ".gitkeep"), None)
        if marker is not None:
            self._empty_marker(marker, inventory.root_entry.oid)
        root_strays = [
            entry.display_path
            for entry in direct
            if entry.local_name != ".gitkeep"
            and not (entry.mode == "040000" and entry.object_type == "tree")
        ]
        if root_strays:
            raise ScreenInputError(
                "unexpected committed path(s) at trees root: " + ", ".join(root_strays),
                tree_oid=inventory.root_entry.oid,
            )
        components = [entry for entry in direct if entry.local_name != ".gitkeep"]
        documents: list[CommittedDocument] = []
        for component in components:
            expected_path = (*component.path, "tree.json")
            immediate = [
                entry
                for entry in inventory.entries
                if len(entry.path) == len(component.path) + 1
                and entry.path[:-1] == component.path
            ]
            exact = inventory.by_path.get(expected_path)
            aliases = [
                entry.display_path
                for entry in immediate
                if entry.local_name != "tree.json"
                and comparison_key(entry.local_name) == comparison_key("tree.json")
            ]
            if aliases:
                raise ScreenInputError(
                    f"case-variant tree.json path for tree {component.local_name}: "
                    + ", ".join(aliases),
                    tree_oid=inventory.root_entry.oid,
                )
            if exact is None:
                raise ScreenInputError(
                    f"committed tree directory {component.display_path}/ is missing exact tree.json",
                    observed=component,
                    tree_oid=inventory.root_entry.oid,
                )
            value = self._json_document(exact, inventory.root_entry.oid)
            documents.append(CommittedDocument(exact.display_path, exact, value))
        return ClassifiedStore(inventory, tuple(documents))

    def _cached_store(self, name: str) -> ClassifiedStore:
        cached = self._store_cache.get(name)
        if isinstance(cached, ScreenInputError):
            raise cached
        if isinstance(cached, ClassifiedStore):
            return cached
        factory: Callable[[], ClassifiedStore]
        if name == MERGE_RECORD_STORE:
            factory = self._classify_merge_records
        elif name == TREE_STORE:
            factory = self._classify_trees
        else:
            raise AssertionError(f"unknown store {name}")
        try:
            value = factory()
        except ScreenInputError as exc:
            self._store_cache[name] = exc
            raise
        self._store_cache[name] = value
        return value

    def preflight(self, names: tuple[str, ...]) -> None:
        first_error: ScreenInputError | None = None
        for name in names:
            try:
                self._cached_store(name)
            except ScreenInputError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def merge_records(self) -> tuple[CommittedDocument, ...]:
        return self._cached_store(MERGE_RECORD_STORE).documents

    def trees(self) -> tuple[CommittedDocument, ...]:
        return self._cached_store(TREE_STORE).documents

    def inventory(self, name: str) -> StoreInventory:
        return self._cached_store(name).inventory

    def store_citation(self, name: str) -> dict:
        cached = self._store_cache.get(name)
        if cached is None:
            return {
                "status": "not-attempted",
                "tree_oid": None,
                "observed": None,
                "error": None,
            }
        if isinstance(cached, ScreenInputError):
            return {
                "status": "error",
                "tree_oid": cached.tree_oid,
                "observed": (
                    cached.observed.observed() if cached.observed is not None else None
                ),
                "error": {"type": type(cached).__name__, "message": str(cached)},
            }
        return {
            "status": "ok",
            "tree_oid": cached.inventory.root_entry.oid,
            "observed": cached.inventory.root_entry.observed(),
            "error": None,
        }

    def citations(self, names: tuple[str, ...]) -> dict:
        return {
            "snapshot": {
                "status": "ok",
                "head_commit": self.snapshot.head_commit,
                "head_tree": self.snapshot.head_tree,
                "error": None,
            },
            "stores": {name: self.store_citation(name) for name in names},
        }


def failed_snapshot_citations(exc: Exception, names: tuple[str, ...]) -> dict:
    return {
        "snapshot": {
            "status": "error",
            "head_commit": None,
            "head_tree": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        },
        "stores": {
            name: {
                "status": "not-attempted",
                "tree_oid": None,
                "observed": None,
                "error": None,
            }
            for name in names
        },
    }


__all__ = [
    "CommittedDocument",
    "Discovery",
    "GIT_TIMEOUT_SECONDS",
    "GitEntry",
    "GitSnapshot",
    "MERGE_RECORD_STORE",
    "ScreenInputError",
    "StoreInventory",
    "TREE_STORE",
    "failed_snapshot_citations",
    "git_read_environment",
]
