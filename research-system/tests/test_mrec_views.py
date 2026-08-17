"""Direct synthetic tests for the committed merge-record selector."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.request

import pytest


_REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(_REPO / "system/instruments/composition-gate"),
    str(_REPO / "system"),
]

from conftest import Sandbox  # noqa: E402
from ht import jsonio  # noqa: E402
from ht.commands import mrec  # noqa: E402
from ht.errors import HtError, HtUsageError  # noqa: E402
from ht.mrec_views import (  # noqa: E402
    MergeRecordEntry,
    load_merge_record_snapshot,
)
import ht.mrec_views as mrec_views  # noqa: E402
import composition_gate.discovery as discovery_module  # noqa: E402
from ht.paths import Root  # noqa: E402


_SOURCE_PYTHONPATH = os.pathsep.join(
    (
        str(_REPO / "system/instruments/composition-gate"),
        str(_REPO / "system"),
    )
)


def _run_mrec_list(sandbox: Sandbox, *args: str):
    """Run only the new view against its co-versioned source dependencies."""

    return sandbox.run(
        "mrec",
        "list",
        *args,
        env_extra={"PYTHONPATH": _SOURCE_PYTHONPATH},
    )


def _gate(verdict: str, number: int) -> dict:
    return {
        "verdict": verdict,
        "date": "2026-07-14",
        "review_ref": f"GR-{number}",
        "review_sha256": f"{number:064x}"[-64:],
        "note": f"Synthetic {verdict} fixture.",
    }


def _record(
    record_id: str,
    *,
    verdict: str | None = None,
    consumed_epoch: int | None = None,
    lane: str = "L4",
) -> dict:
    return {
        "id": record_id,
        "candidate_ref": f"tree#{lane}/node#{record_id.removeprefix('MR-')}",
        "lane_verdict": "lane-pass",
        "scope": {"lane": lane, "seats": [], "surfaces": [], "globs": []},
        "screen": {
            "results": [],
            "output_ref": None,
            "log_ref": None,
            "log_sha256": None,
            "output_sha256": None,
            "computed": None,
            "head_commit": None,
            "head_tree": None,
            "config_hash": None,
            "engine_version": None,
        },
        "gate_verdict": None if verdict is None else _gate(verdict, int(record_id[3:])),
        "watch_link": None,
        "created": "2026-07-14",
        "consumed_epoch": consumed_epoch,
    }


def _commit_documents(
    sandbox: Sandbox, documents: dict[str, object], message: str = "synthetic records"
) -> None:
    for relative, value in documents.items():
        path = sandbox.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(jsonio.dumps(value), encoding="utf-8")
    added = sandbox.git("add", "--", *documents)
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", message)
    assert committed.returncode == 0, committed.stderr


def _commit_records(sandbox: Sandbox, records: list[dict]) -> None:
    _commit_documents(
        sandbox,
        {
            f"tier1/merge-records/{record['id']}.json": record
            for record in records
        },
    )


def _ids(entries: tuple[MergeRecordEntry, ...]) -> list[str]:
    return [entry.id for entry in entries]


def _tree_lines(sandbox: Sandbox, treeish: str) -> list[str]:
    result = sandbox.git("ls-tree", "-z", "--full-tree", treeish)
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.rstrip("\0").split("\0") if line]


def _mktree(sandbox: Sandbox, lines: list[str]) -> str:
    result = sandbox.git("mktree", input_text="\n".join(lines) + "\n")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _replace_tree_entry(lines: list[str], name: str, replacement: str) -> list[str]:
    result = []
    for line in lines:
        if line.rsplit("\t", 1)[1] == name:
            result.append(replacement)
        else:
            result.append(line)
    assert len(result) == len(lines)
    return result


def _commit_tree(sandbox: Sandbox, tree_oid: str, message: str) -> None:
    parent = sandbox.git("rev-parse", "HEAD")
    assert parent.returncode == 0, parent.stderr
    committed = sandbox.git(
        "commit-tree", tree_oid, "-p", parent.stdout.strip(), input_text=message + "\n"
    )
    assert committed.returncode == 0, committed.stderr
    updated = sandbox.git("update-ref", "HEAD", committed.stdout.strip())
    assert updated.returncode == 0, updated.stderr


def _add_store_alias(sandbox: Sandbox, alias: str) -> None:
    merge_records_oid = sandbox.git("rev-parse", "HEAD:tier1/merge-records")
    assert merge_records_oid.returncode == 0, merge_records_oid.stderr
    tier1_entries = _tree_lines(sandbox, "HEAD:tier1")
    tier1_entries.append(
        f"040000 tree {merge_records_oid.stdout.strip()}\t{alias}"
    )
    tier1_oid = _mktree(sandbox, tier1_entries)
    root_entries = _replace_tree_entry(
        _tree_lines(sandbox, "HEAD"),
        "tier1",
        f"040000 tree {tier1_oid}\ttier1",
    )
    _commit_tree(sandbox, _mktree(sandbox, root_entries), f"alias {alias}")


def _physical_state(sandbox: Sandbox) -> tuple:
    def git_output(*args: str) -> str:
        result = sandbox.git(*args)
        assert result.returncode == 0, result.stderr
        return result.stdout

    var_rows: list[tuple[str, bytes]] = []
    var_root = sandbox.root / "var"
    if var_root.exists():
        for directory, _directories, files in os.walk(var_root):
            for filename in files:
                path = Path(directory) / filename
                var_rows.append((path.relative_to(sandbox.root).as_posix(), path.read_bytes()))
    return (
        git_output("rev-parse", "HEAD"),
        git_output("diff", "--raw"),
        git_output("diff", "--cached", "--raw"),
        git_output("status", "--porcelain=v1", "--untracked-files=all"),
        tuple(sorted(var_rows)),
    )


def _clone_partial(source: Path, target: Path) -> None:
    cloned = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            "--filter=blob:none",
            source.as_uri(),
            str(target),
        ],
        text=True,
        capture_output=True,
    )
    assert cloned.returncode == 0, cloned.stderr


def _partial_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        env=env,
        text=True,
        capture_output=True,
    )


def _partial_state(root: Path) -> tuple[str, str, str, tuple[str, ...]]:
    head = _partial_git(root, "rev-parse", "HEAD")
    status = _partial_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    remotes = _partial_git(root, "remote", "-v")
    missing = _partial_git(root, "rev-list", "--objects", "--missing=print", "HEAD")
    for result in (head, status, remotes, missing):
        assert result.returncode == 0, result.stderr
    return (
        head.stdout,
        status.stdout,
        remotes.stdout,
        tuple(sorted(row for row in missing.stdout.splitlines() if row.startswith("?"))),
    )


_LIST_HEADER = "id  candidate_ref  lane  verdict  created  consumed_epoch"
_LIST_PARTITIONS = (
    "awaiting-verdict",
    "land-ready",
    "verdict-issued/unconsumed",
    "consumed",
)


def test_cli_mrec_list_default_all_is_role_free_and_exactly_grouped(
    sandbox: Sandbox,
):
    records = [
        _record("MR-1"),
        _record("MR-2", verdict="land"),
        _record("MR-3", verdict="hold"),
        _record("MR-4", verdict="land", consumed_epoch=8),
    ]
    _commit_records(sandbox, records)
    before = _physical_state(sandbox)

    result = _run_mrec_list(sandbox)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == (
        "awaiting-verdict\n"
        f"{_LIST_HEADER}\n"
        "MR-1  tree#L4/node#1  L4  —  2026-07-14  —\n"
        "land-ready\n"
        f"{_LIST_HEADER}\n"
        "MR-2  tree#L4/node#2  L4  land  2026-07-14  —\n"
        "verdict-issued/unconsumed\n"
        f"{_LIST_HEADER}\n"
        "MR-3  tree#L4/node#3  L4  hold  2026-07-14  —\n"
        "consumed\n"
        f"{_LIST_HEADER}\n"
        "MR-4  tree#L4/node#4  L4  land  2026-07-14  8\n"
    )
    assert _physical_state(sandbox) == before


def test_cli_mrec_list_named_statuses_preserve_selector_overlap_and_order(
    sandbox: Sandbox,
):
    _commit_records(
        sandbox,
        [
            _record("MR-1"),
            _record("MR-2", verdict="land"),
            _record("MR-3", verdict="hold"),
            _record("MR-4", verdict="land", consumed_epoch=4),
            _record("MR-5", verdict="land", consumed_epoch=4),
        ],
    )
    expected = {
        "pending": ["MR-1", "MR-2"],
        "landed": ["MR-2", "MR-3"],
        "consumed": ["MR-5", "MR-4"],
    }

    for status, ids in expected.items():
        result = _run_mrec_list(sandbox, "--status", status)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert lines[0] == _LIST_HEADER
        assert [line.split("  ", 1)[0] for line in lines[1:]] == ids


def test_cli_mrec_list_json_is_exact_selected_record_objects(sandbox: Sandbox):
    records = [
        _record("MR-1"),
        _record("MR-2", verdict="land"),
        _record("MR-3", verdict="hold"),
        _record("MR-4", verdict="land", consumed_epoch=8),
    ]
    _commit_records(sandbox, records)

    result = _run_mrec_list(sandbox, "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == records
    assert all(
        not {"partition", "pending", "landed", "consumed"} & record.keys()
        for record in json.loads(result.stdout)
    )


def test_cli_mrec_list_last_zero_high_and_global_repartition(sandbox: Sandbox):
    _commit_records(
        sandbox,
        [
            _record("MR-1"),
            _record("MR-2", verdict="land"),
            _record("MR-3", verdict="hold"),
            _record("MR-4", verdict="land", consumed_epoch=4),
            _record("MR-5", verdict="hold"),
            _record("MR-6", verdict="land", consumed_epoch=5),
        ],
    )

    empty = _run_mrec_list(sandbox, "--last", "0")
    assert empty.returncode == 0, empty.stderr
    assert empty.stdout == "".join(
        f"{partition}\n{_LIST_HEADER}\n" for partition in _LIST_PARTITIONS
    )

    default = _run_mrec_list(sandbox)
    high = _run_mrec_list(sandbox, "--last", "99")
    assert high.returncode == 0, high.stderr
    assert high.stdout == default.stdout

    limited = _run_mrec_list(sandbox, "--last", "4")
    assert limited.returncode == 0, limited.stderr
    lines = limited.stdout.splitlines()
    assert [line.split("  ", 1)[0] for line in lines if line.startswith("MR-")] == [
        "MR-3",
        "MR-5",
        "MR-6",
        "MR-4",
    ]
    assert "MR-1" not in limited.stdout
    assert "MR-2" not in limited.stdout


def test_cli_mrec_list_last_rejects_negative_and_non_integer(sandbox: Sandbox):
    negative = _run_mrec_list(sandbox, "--last", "-1")
    assert negative.returncode != 0
    assert negative.stdout == ""
    assert "REJECTED: merge-record --last must be a non-negative integer" in negative.stderr

    textual = _run_mrec_list(sandbox, "--last", "K")
    assert textual.returncode == 2
    assert textual.stdout == ""
    assert "invalid int value" in textual.stderr


def test_mrec_list_loads_once_and_selects_once_for_all(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    _commit_records(sandbox, [_record("MR-1")])
    original_load = mrec_views.load_merge_record_snapshot
    original_select = mrec_views.MergeRecordSnapshot.select
    load_calls: list[Path] = []
    select_calls: list[tuple[str, int | None]] = []

    def load_spy(root: str | Path):
        load_calls.append(Path(root))
        return original_load(root)

    def select_spy(snapshot, status="all", last=None):
        select_calls.append((status, last))
        return original_select(snapshot, status=status, last=last)

    monkeypatch.setattr(mrec_views, "load_merge_record_snapshot", load_spy)
    monkeypatch.setattr(mrec_views.MergeRecordSnapshot, "select", select_spy)

    assert mrec.list_records(Root(sandbox.root), status="all") == 0
    capsys.readouterr()
    assert load_calls == [sandbox.root]
    assert select_calls == [("all", None)]


def test_cli_mrec_list_ignores_dirty_records_and_preserves_state(sandbox: Sandbox):
    _commit_records(sandbox, [_record("MR-1")])
    records_dir = sandbox.root / "tier1/merge-records"
    (records_dir / "MR-1.json").write_text(
        jsonio.dumps(_record("MR-1", verdict="hold")), encoding="utf-8"
    )
    (records_dir / "MR-99.json").write_text(
        jsonio.dumps(_record("MR-99")), encoding="utf-8"
    )
    before = _physical_state(sandbox)

    result = _run_mrec_list(sandbox)

    assert result.returncode == 0, result.stderr
    assert "MR-1  tree#L4/node#1  L4  —  2026-07-14  —" in result.stdout
    assert "MR-99" not in result.stdout
    assert "hold" not in result.stdout
    assert _physical_state(sandbox) == before


def test_cli_mrec_list_malformed_committed_store_has_no_partial_stdout(
    sandbox: Sandbox,
):
    _commit_records(sandbox, [_record("MR-1")])
    _commit_documents(
        sandbox,
        {"tier1/merge-records/MR-2.json": "{"},
        "malformed committed record",
    )
    before = _physical_state(sandbox)

    result = _run_mrec_list(sandbox)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.startswith("REJECTED:")
    assert _physical_state(sandbox) == before


def test_membership_matrix_overlap_and_all_partition(sandbox: Sandbox):
    records = [
        _record("MR-1"),
        _record("MR-2", verdict="land"),
        _record("MR-3", verdict="hold"),
        _record("MR-4", verdict="consolidate-first"),
        _record("MR-5", verdict="land-after-X"),
        _record("MR-6", verdict="escalate-stuck"),
        _record("MR-7", verdict="escalate-to-user"),
        _record("MR-8", verdict="land", consumed_epoch=4),
        _record("MR-9", verdict="land", consumed_epoch=4),
        _record("MR-10", verdict="land", consumed_epoch=5),
    ]
    _commit_records(sandbox, records)

    snapshot = load_merge_record_snapshot(sandbox.root)

    assert _ids(snapshot.select("pending")) == ["MR-1", "MR-2"]
    assert _ids(snapshot.select("landed")) == [
        "MR-2",
        "MR-3",
        "MR-4",
        "MR-5",
        "MR-6",
        "MR-7",
    ]
    assert _ids(snapshot.select("consumed")) == ["MR-10", "MR-9", "MR-8"]
    assert _ids(snapshot.select("all")) == [
        "MR-1",
        "MR-2",
        "MR-3",
        "MR-4",
        "MR-5",
        "MR-6",
        "MR-7",
        "MR-10",
        "MR-9",
        "MR-8",
    ]

    pending = set(_ids(snapshot.select("pending")))
    landed = set(_ids(snapshot.select("landed")))
    consumed = set(_ids(snapshot.select("consumed")))
    assert pending & landed == {"MR-2"}
    assert not pending & consumed
    assert not landed & consumed
    assert set(_ids(snapshot.select("all"))) == pending | landed | consumed
    assert len(_ids(snapshot.select("all"))) == len(set(_ids(snapshot.select("all"))))

    partitions = {
        entry.id: entry.partition for entry in snapshot.entries
    }
    assert partitions == {
        "MR-1": "awaiting-verdict",
        "MR-2": "land-ready",
        "MR-3": "verdict-issued/unconsumed",
        "MR-4": "verdict-issued/unconsumed",
        "MR-5": "verdict-issued/unconsumed",
        "MR-6": "verdict-issued/unconsumed",
        "MR-7": "verdict-issued/unconsumed",
        "MR-8": "consumed",
        "MR-9": "consumed",
        "MR-10": "consumed",
    }
    assert _ids(snapshot.unconsumed()) == [f"MR-{number}" for number in range(1, 8)]
    assert _ids(snapshot.select_unconsumed()) == _ids(snapshot.unconsumed())


def test_empty_store_and_mr_zero_numeric_entry_order(sandbox: Sandbox):
    empty = load_merge_record_snapshot(sandbox.root)
    assert empty.entries == ()
    assert empty.select("all") == ()
    assert empty.unconsumed() == ()

    # Commit deliberately in an order that differs from both lexical and
    # numeric order.  The exported entries boundary itself is numeric.
    _commit_records(
        sandbox,
        [_record("MR-10"), _record("MR-0"), _record("MR-2")],
    )
    snapshot = load_merge_record_snapshot(sandbox.root)
    assert _ids(snapshot.entries) == ["MR-0", "MR-2", "MR-10"]
    assert [entry.ordinal for entry in snapshot.entries] == [0, 2, 10]
    assert _ids(snapshot.select("pending")) == ["MR-0", "MR-2", "MR-10"]


def test_all_last_and_each_status_last_semantics(sandbox: Sandbox):
    _commit_records(
        sandbox,
        [
            _record("MR-1"),
            _record("MR-2", verdict="land"),
            _record("MR-3", verdict="hold"),
            _record("MR-4", verdict="consolidate-first"),
            _record("MR-5", verdict="land-after-X"),
            _record("MR-6", verdict="escalate-stuck"),
            _record("MR-7", verdict="escalate-to-user"),
            _record("MR-8", verdict="land", consumed_epoch=4),
            _record("MR-9", verdict="land", consumed_epoch=4),
            _record("MR-10", verdict="land", consumed_epoch=5),
        ],
    )
    snapshot = load_merge_record_snapshot(sandbox.root)

    assert _ids(snapshot.select("pending", last=0)) == []
    assert _ids(snapshot.select("pending", last=1)) == ["MR-2"]
    assert _ids(snapshot.select("pending", last=2)) == ["MR-1", "MR-2"]
    assert _ids(snapshot.select("pending", last=99)) == ["MR-1", "MR-2"]

    assert _ids(snapshot.select("landed", last=0)) == []
    assert _ids(snapshot.select("landed", last=1)) == ["MR-7"]
    assert _ids(snapshot.select("landed", last=3)) == ["MR-5", "MR-6", "MR-7"]
    assert _ids(snapshot.select("landed", last=99)) == [
        "MR-2",
        "MR-3",
        "MR-4",
        "MR-5",
        "MR-6",
        "MR-7",
    ]

    assert _ids(snapshot.select("consumed", last=0)) == []
    assert _ids(snapshot.select("consumed", last=1)) == ["MR-10"]
    assert _ids(snapshot.select("consumed", last=2)) == ["MR-10", "MR-9"]
    assert _ids(snapshot.select("consumed", last=99)) == ["MR-10", "MR-9", "MR-8"]

    assert _ids(snapshot.select("all", last=0)) == []
    assert _ids(snapshot.select("all", last=1)) == ["MR-10"]
    assert _ids(snapshot.select("all", last=3)) == ["MR-10", "MR-9", "MR-8"]
    assert _ids(snapshot.select("all", last=4)) == ["MR-7", "MR-10", "MR-9", "MR-8"]
    assert _ids(snapshot.select("all", last=7)) == [
        "MR-4",
        "MR-5",
        "MR-6",
        "MR-7",
        "MR-10",
        "MR-9",
        "MR-8",
    ]
    assert _ids(snapshot.select("all", last=99)) == _ids(snapshot.select("all"))

    with pytest.raises(HtUsageError):
        snapshot.select("unknown")
    with pytest.raises(HtUsageError):
        snapshot.select("all", last=-1)
    with pytest.raises(HtUsageError):
        snapshot.select("all", last=True)
    with pytest.raises(HtUsageError):
        snapshot.select("all", last=1.0)  # type: ignore[arg-type]


def test_entries_are_frozen_and_as_dict_is_fresh_full_record(sandbox: Sandbox):
    record = _record("MR-1", verdict="land")
    _commit_records(sandbox, [record])
    entry = load_merge_record_snapshot(sandbox.root).entries[0]

    assert dataclasses.is_dataclass(entry)
    assert entry.canonical_bytes == jsonio.dumps(record).encode("utf-8")
    assert entry.record_bytes == entry.canonical_bytes
    assert entry.id == "MR-1"
    assert entry.ordinal == 1
    assert entry.partition == "land-ready"
    assert entry.pending is True
    assert entry.landed is True
    assert entry.consumed is False

    first = entry.as_dict()
    second = entry.as_dict()
    assert first == record
    assert second == record
    assert first is not second
    assert first["scope"] is not second["scope"]
    first["scope"]["lane"] = "mutated"
    assert entry.as_dict() == record
    assert "pending" not in first
    assert "landed" not in first
    assert "consumed" not in first
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.id = "MR-2"  # type: ignore[misc]


def test_one_discovery_snapshot_worktree_invisibility_and_read_only_state(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
):
    original = _record("MR-1", verdict="land")
    _commit_records(sandbox, [original])
    before = _physical_state(sandbox)
    calls: list[Path] = []
    capture = mrec_views.Discovery.capture

    def spy(root: str | Path):
        calls.append(Path(root))
        return capture(root)

    monkeypatch.setattr(mrec_views.Discovery, "capture", staticmethod(spy))
    snapshot = load_merge_record_snapshot(sandbox.root)
    assert len(calls) == 1
    assert _physical_state(sandbox) == before

    # These are deliberately worktree-only and must not affect the pinned view.
    (sandbox.root / "tier1/merge-records/MR-1.json").write_text(
        jsonio.dumps(_record("MR-1", verdict="hold")), encoding="utf-8"
    )
    (sandbox.root / "tier1/merge-records/MR-99.json").write_text(
        jsonio.dumps(_record("MR-99")), encoding="utf-8"
    )
    dirty_state = _physical_state(sandbox)
    assert _ids(snapshot.select("all")) == ["MR-1"]
    assert snapshot.entries[0].as_dict() == original
    assert len(calls) == 1
    assert _physical_state(sandbox) == dirty_state

    # A new capture still reads the committed bytes, despite the dirty files.
    recaptured = load_merge_record_snapshot(sandbox.root)
    assert _ids(recaptured.select("all")) == ["MR-1"]
    assert recaptured.entries[0].as_dict() == original
    assert len(calls) == 2
    assert _physical_state(sandbox) == dirty_state


@pytest.mark.parametrize("mutation", ["malformed", "deleted"])
def test_worktree_schema_is_invisible_before_first_selector_call(
    sandbox: Sandbox, mutation: str
):
    record = _record("MR-1")
    _commit_records(sandbox, [record])
    schema_path = sandbox.root / "system/schemas/merge_record.schema.json"
    if mutation == "malformed":
        schema_path.write_text("{", encoding="utf-8")
    else:
        schema_path.unlink()
    dirty_state = _physical_state(sandbox)

    snapshot = load_merge_record_snapshot(sandbox.root)

    assert _ids(snapshot.entries) == ["MR-1"]
    assert snapshot.entries[0].as_dict() == record
    assert _physical_state(sandbox) == dirty_state


def test_subsequent_committed_schema_change_is_seen_in_same_process(
    sandbox: Sandbox,
):
    record = _record("MR-1")
    record["created"] = ""
    _commit_records(sandbox, [record])

    assert _ids(load_merge_record_snapshot(sandbox.root).entries) == ["MR-1"]

    schema_path = sandbox.root / "system/schemas/merge_record.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["created"]["minLength"] = 1
    schema_path.write_text(jsonio.dumps(schema), encoding="utf-8")
    added = sandbox.git("add", "--", "system/schemas/merge_record.schema.json")
    assert added.returncode == 0, added.stderr
    committed = sandbox.git(
        "commit", "--no-verify", "-m", "tighten synthetic merge record schema"
    )
    assert committed.returncode == 0, committed.stderr

    with pytest.raises(HtError, match="schema-nonconforming.*created"):
        load_merge_record_snapshot(sandbox.root)


@pytest.mark.parametrize("construction", ["malformed", "missing", "symlink"])
def test_malformed_missing_or_unsupported_committed_schema_fails_closed(
    sandbox: Sandbox, construction: str
):
    _commit_records(sandbox, [_record("MR-1")])
    schema_path = sandbox.root / "system/schemas/merge_record.schema.json"
    if construction == "malformed":
        schema_path.write_text("{", encoding="utf-8")
        added = sandbox.git("add", "--", "system/schemas/merge_record.schema.json")
    elif construction == "missing":
        schema_path.unlink()
        added = sandbox.git("rm", "--", "system/schemas/merge_record.schema.json")
    else:
        schema_path.unlink()
        schema_path.symlink_to("merge_record.schema.synthetic-target")
        added = sandbox.git("add", "--", "system/schemas/merge_record.schema.json")
    assert added.returncode == 0, added.stderr
    committed = sandbox.git(
        "commit", "--no-verify", "-m", f"bad committed schema {construction}"
    )
    assert committed.returncode == 0, committed.stderr

    with pytest.raises(HtError, match="merge-record committed schema rejected"):
        load_merge_record_snapshot(sandbox.root)


def test_direct_schema_nonconforming_record_fails_closed(sandbox: Sandbox):
    record = _record("MR-1")
    del record["created"]
    _commit_documents(
        sandbox,
        {"tier1/merge-records/MR-1.json": record},
        "schema nonconforming record",
    )

    with pytest.raises(HtError, match="schema-nonconforming.*created"):
        load_merge_record_snapshot(sandbox.root)


@pytest.mark.parametrize("alias", ["MERGE-RECORDS", "merge-recordſ"])
def test_case_and_unicode_store_aliases_fail_closed(sandbox: Sandbox, alias: str):
    _commit_records(sandbox, [_record("MR-1")])
    _add_store_alias(sandbox, alias)

    with pytest.raises(HtError, match="merge-record snapshot discovery failed"):
        load_merge_record_snapshot(sandbox.root)


def test_nested_stray_fails_closed(sandbox: Sandbox):
    _commit_records(sandbox, [_record("MR-1")])
    _commit_documents(
        sandbox,
        {"tier1/merge-records/nested/stray.json": "{}"},
        "nested stray",
    )
    with pytest.raises(HtError, match="merge-record snapshot discovery failed"):
        load_merge_record_snapshot(sandbox.root)


@pytest.mark.parametrize("mode", ["symlink", "executable", "submodule"])
def test_symlink_executable_and_submodule_modes_fail_closed(
    sandbox: Sandbox, mode: str
):
    _commit_records(sandbox, [_record("MR-1")])
    path = sandbox.root / "tier1/merge-records/MR-2.json"
    if mode == "symlink":
        path.symlink_to("MR-1.json")
        added = sandbox.git("add", "--", "tier1/merge-records/MR-2.json")
        assert added.returncode == 0, added.stderr
    elif mode == "executable":
        path = sandbox.root / "tier1/merge-records/MR-1.json"
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        added = sandbox.git("add", "--", "tier1/merge-records/MR-1.json")
        assert added.returncode == 0, added.stderr
    else:
        head = sandbox.git("rev-parse", "HEAD")
        assert head.returncode == 0, head.stderr
        added = sandbox.git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head.stdout.strip()},tier1/merge-records/submodule",
        )
        assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", f"bad {mode} mode")
    assert committed.returncode == 0, committed.stderr

    with pytest.raises(HtError, match="merge-record snapshot discovery failed"):
        load_merge_record_snapshot(sandbox.root)


@pytest.mark.parametrize("body", ["{", "[]"])
def test_malformed_and_non_object_json_fail_closed(sandbox: Sandbox, body: str):
    _commit_records(sandbox, [_record("MR-1")])
    _commit_documents(sandbox, {"tier1/merge-records/MR-1.json": body}, "bad JSON")

    with pytest.raises(HtError, match="merge-record snapshot discovery failed"):
        load_merge_record_snapshot(sandbox.root)


def test_path_body_mismatch_and_leading_zero_numeric_alias_fail_closed(
    sandbox: Sandbox,
):
    _commit_documents(
        sandbox,
        {"tier1/merge-records/MR-1.json": _record("MR-2")},
        "path body mismatch",
    )
    with pytest.raises(HtError, match="merge-record snapshot discovery failed"):
        load_merge_record_snapshot(sandbox.root)

    # A separate disposable commit isolates the canonical-ID rejection.
    # MR-01 is a numeric alias of MR-1 and is rejected as non-canonical.  An
    # exact duplicate canonical ordinal would require a duplicate Git filename,
    # which a tree object cannot represent; the loader retains a defensive
    # seen-ordinal guard for that structurally unreachable case.
    second = sandbox.root / "tier1/merge-records/MR-1.json"
    second.write_text(jsonio.dumps(_record("MR-1")), encoding="utf-8")
    leading = sandbox.root / "tier1/merge-records/MR-01.json"
    leading.write_text(jsonio.dumps(_record("MR-01")), encoding="utf-8")
    added = sandbox.git(
        "add", "--", "tier1/merge-records/MR-1.json", "tier1/merge-records/MR-01.json"
    )
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "leading zero and collision")
    assert committed.returncode == 0, committed.stderr
    with pytest.raises(HtError, match="non-canonical numeric id"):
        load_merge_record_snapshot(sandbox.root)


def test_consumed_non_land_lifecycle_fails_closed(sandbox: Sandbox):
    _commit_records(sandbox, [_record("MR-1", verdict="hold", consumed_epoch=1)])

    with pytest.raises(HtError, match="exact gate verdict 'land'"):
        load_merge_record_snapshot(sandbox.root)


def test_explicit_root_and_every_git_read_ignore_inherited_git_routing(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    other_root = tmp_path / "other-repository"
    other_root.mkdir()
    other = Sandbox(other_root)
    initialized = other.run("root", "init", role="harness")
    assert initialized.returncode == 0, initialized.stderr
    _commit_records(other, [_record("MR-77")])

    routed = subprocess.run(
        [
            "git",
            "-C",
            str(sandbox.root),
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
            "--",
            "tier1/merge-records",
        ],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_DIR": str(other.root / ".git"),
            "GIT_WORK_TREE": str(other.root),
        },
        text=True,
        capture_output=True,
    )
    assert routed.returncode == 0, routed.stderr
    assert "tier1/merge-records/MR-77.json" in routed.stdout

    malicious = {
        "GIT_DIR": str(other.root / ".git"),
        "GIT_WORK_TREE": str(other.root),
        "GIT_COMMON_DIR": str(other.root / ".git"),
        "GIT_OBJECT_DIRECTORY": str(other.root / ".git/objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other.root / ".git/objects"),
        "GIT_INDEX_FILE": str(other.root / ".git/index"),
        "GIT_GRAFT_FILE": str(other.root / ".git/info/grafts"),
        "GIT_SHALLOW_FILE": str(other.root / ".git/shallow"),
        "GIT_REPLACE_REF_BASE": "refs/hostile-replacements/",
        "GIT_NAMESPACE": "hostile",
        "GIT_QUARANTINE_PATH": str(other.root / ".git/objects"),
        "GIT_PREFIX": "redirected/",
        "GIT_IMPLICIT_WORK_TREE": "1",
        "GIT_EXEC_PATH": str(other.root),
        "GIT_CONFIG": str(other.root / ".git/config"),
        "GIT_CONFIG_SYSTEM": str(other.root / ".git/config"),
        "GIT_CONFIG_GLOBAL": str(other.root / ".git/config"),
        "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.repositoryformatversion",
        "GIT_CONFIG_VALUE_0": "999",
        "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
    }
    for key, value in malicious.items():
        monkeypatch.setenv(key, value)

    popen_envs: list[dict[str, str]] = []
    run_envs: list[dict[str, str]] = []
    original_popen = discovery_module.subprocess.Popen
    original_run = mrec_views.subprocess.run

    def popen_spy(*args, **kwargs):
        popen_envs.append(dict(kwargs["env"]))
        return original_popen(*args, **kwargs)

    def run_spy(*args, **kwargs):
        run_envs.append(dict(kwargs["env"]))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(discovery_module.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(mrec_views.subprocess, "run", run_spy)

    # Root A is empty.  The inherited GIT_DIR construction demonstrably points
    # ordinary Git at root B, but neither Discovery nor the schema reader may
    # observe B's MR-77.
    assert _ids(load_merge_record_snapshot(sandbox.root).entries) == []
    assert popen_envs
    assert run_envs
    for env in [*popen_envs, *run_envs]:
        assert env["GIT_NO_LAZY_FETCH"] == "1"
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_COUNT"] == "0"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert not (set(malicious) - {
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
        }) & set(env)
    for key, value in malicious.items():
        assert os.environ[key] == value


def _commit_schema_ref(sandbox: Sandbox, uri: str) -> None:
    schema_path = sandbox.root / "system/schemas/merge_record.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["allOf"] = [{"$ref": uri}]
    schema_path.write_text(jsonio.dumps(schema), encoding="utf-8")
    added = sandbox.git("add", "--", "system/schemas/merge_record.schema.json")
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "external schema ref")
    assert committed.returncode == 0, committed.stderr


def test_closed_schema_registry_never_reads_mutable_file_resource(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _commit_records(sandbox, [_record("MR-1")])
    external = tmp_path / "external-schema.json"
    external.write_text('{"type":"object"}\n', encoding="utf-8")
    _commit_schema_ref(sandbox, external.as_uri())

    constructed: list[object] = []

    def forbidden_entry(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("entry construction preceded closed-ref rejection")

    monkeypatch.setattr(mrec_views, "MergeRecordEntry", forbidden_entry)
    with pytest.raises(HtError) as first:
        load_merge_record_snapshot(sandbox.root)
    external.write_text('{"not":{"a":"schema"}}\n', encoding="utf-8")
    with pytest.raises(HtError) as second:
        load_merge_record_snapshot(sandbox.root)

    assert str(first.value) == str(second.value)
    assert "committed schema could not validate" in str(first.value)
    assert "_WrappedReferencingError" in str(first.value)
    assert constructed == []


def test_closed_schema_registry_resolves_captured_self_resource(
    sandbox: Sandbox,
):
    record = _record("MR-1")
    _commit_records(sandbox, [record])
    schema_path = sandbox.root / "system/schemas/merge_record.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    scope_schema = schema["properties"]["scope"]
    schema["$defs"] = {"capturedScope": scope_schema}
    schema["properties"]["scope"] = {
        "$ref": f"{schema['$id']}#/$defs/capturedScope"
    }
    schema_path.write_text(jsonio.dumps(schema), encoding="utf-8")
    added = sandbox.git("add", "--", "system/schemas/merge_record.schema.json")
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "captured self ref")
    assert committed.returncode == 0, committed.stderr

    snapshot = load_merge_record_snapshot(sandbox.root)
    assert _ids(snapshot.entries) == ["MR-1"]
    assert snapshot.entries[0].as_dict() == record


def test_closed_schema_registry_http_ref_uses_deny_seam_without_outbound_io(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    _commit_records(sandbox, [_record("MR-1")])
    uri = "http://127.0.0.1:9/mutable-schema.json"
    _commit_schema_ref(sandbox, uri)
    requested: list[str] = []
    original_deny = mrec_views._deny_uncaptured_schema_resource

    def deny_spy(value: str):
        requested.append(value)
        return original_deny(value)

    def outbound_forbidden(*_args, **_kwargs):
        raise AssertionError("closed registry attempted outbound retrieval")

    monkeypatch.setattr(mrec_views, "_deny_uncaptured_schema_resource", deny_spy)
    monkeypatch.setattr(urllib.request, "urlopen", outbound_forbidden)

    with pytest.raises(HtError, match="committed schema could not validate"):
        load_merge_record_snapshot(sandbox.root)
    assert requested == [uri]


@pytest.mark.parametrize(
    "invalid_epoch",
    [
        pytest.param(1.0, id="integral-float"),
        pytest.param(-0.0, id="negative-zero-float"),
        pytest.param(1e3, id="exponent-float"),
        pytest.param(True, id="boolean"),
        pytest.param(-1, id="negative-int"),
    ],
)
def test_consumed_epoch_requires_exact_nonnegative_int_before_any_entry(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    invalid_epoch: object,
):
    invalid = _record("MR-2", verdict="land")
    invalid["consumed_epoch"] = invalid_epoch
    _commit_records(sandbox, [_record("MR-1"), invalid])
    constructed: list[object] = []

    def forbidden_entry(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("entry construction preceded epoch preflight")

    monkeypatch.setattr(mrec_views, "MergeRecordEntry", forbidden_entry)
    with pytest.raises(HtError) as error:
        load_merge_record_snapshot(sandbox.root)
    assert type(error.value) is HtError
    assert "consumed_epoch must be null or an exact non-negative integer" in str(
        error.value
    )
    assert constructed == []


def test_no_lazy_fetch_rejects_promisor_blob_without_repository_mutation(
    sandbox: Sandbox,
    tmp_path: Path,
):
    _commit_records(sandbox, [_record("MR-1")])
    configured = sandbox.git("config", "uploadpack.allowFilter", "true")
    assert configured.returncode == 0, configured.stderr
    schema_oid = sandbox.git("rev-parse", "HEAD:system/schemas/merge_record.schema.json")
    assert schema_oid.returncode == 0, schema_oid.stderr
    oid = schema_oid.stdout.strip()

    bad = tmp_path / "unhardened-partial"
    hardened = tmp_path / "hardened-partial"
    _clone_partial(sandbox.root, bad)
    _clone_partial(sandbox.root, hardened)
    assert f"?{oid}" in _partial_state(bad)[3]
    assert f"?{oid}" in _partial_state(hardened)[3]

    # Calibrate the fixture: the ordinary read lazily fetches and persists the
    # promised blob.  This is intentionally performed only in the bad clone.
    fetched = subprocess.run(
        ["git", "-C", str(bad), "cat-file", "blob", oid],
        text=True,
        capture_output=True,
    )
    assert fetched.returncode == 0, fetched.stderr
    assert f"?{oid}" not in _partial_state(bad)[3]

    before = _partial_state(hardened)
    with pytest.raises(HtError, match="merge-record snapshot discovery failed"):
        load_merge_record_snapshot(hardened)
    after = _partial_state(hardened)
    assert after == before
    assert f"?{oid}" in after[3]


def test_negative_checks_are_calibrated_against_a_bad_disposable_construction(
    sandbox: Sandbox,
):
    _commit_records(sandbox, [_record("MR-1")])
    link = sandbox.root / "tier1/merge-records/MR-2.json"
    link.symlink_to("MR-1.json")
    added = sandbox.git("add", "--", "tier1/merge-records/MR-2.json")
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "calibration symlink")
    assert committed.returncode == 0, committed.stderr

    # The deliberately bad worktree-following construction appears valid to a
    # naive loader; the committed-object selector rejects the same construction.
    assert json.loads(link.read_text(encoding="utf-8")) == _record("MR-1")
    with pytest.raises(HtError, match="merge-record snapshot discovery failed"):
        load_merge_record_snapshot(sandbox.root)
