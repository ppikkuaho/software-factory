"""Item 1 federation substrate: book paths, union helpers, and mutex."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ht import classify, ledger_index
from ht.commands import _common, ledger, validate
from ht.commands._common import Ctx
from ht.errors import HtError
from ht.mutex import global_mutex
from ht.paths import Root


SECTIONS = ("user", "research", "observatory")


def _ctx(path: Path) -> Ctx:
    return Ctx(root=Root(path), role="harness")


def _seed_entry(
    root: Root,
    *,
    book: str,
    section: str,
    entry_id: str,
) -> Path:
    path = root.ledger_entry(book, section, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": entry_id}), encoding="utf-8")
    return path


def _seed_ledger_doc(
    root: Root,
    *,
    book: str,
    section: str,
    entry_id: str,
    text: str,
) -> dict:
    doc = {
        "id": entry_id,
        "section": section,
        "text": text,
        "proposed_by": "observation#seed",
        "support_count": 0,
        "echoes": [],
        "cross_refs": [],
        "dedup_log": [],
        "status": {"state": "open", "ref": None, "reason": None},
        "anchors": [],
        "component": None,
        "lane": book if book != "top" else None,
    }
    path = root.ledger_entry(book, section, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_root_init_creates_top_book_without_legacy_section_dirs(sandbox) -> None:
    ledger = sandbox.root / "ledger"

    assert all((ledger / "top" / section).is_dir() for section in SECTIONS)
    assert all(not (ledger / section).exists() for section in SECTIONS)


@pytest.mark.parametrize("section", SECTIONS)
def test_classify_extracts_ledger_book_and_section(section: str) -> None:
    rel = f"ledger/L4/{section}/L-12.json"

    assert classify.doc_type(rel) == "ledger_entry"
    assert classify.ledger_book(rel) == "L4"
    assert classify.ledger_section(rel) == section


def test_root_exposes_book_aware_ledger_paths(tmp_path: Path) -> None:
    root = Root(tmp_path / "research")

    assert root.ledger_book_dir("L3") == root.path / "ledger" / "L3"
    assert root.ledger_entry("L3", "research", "L-9") == (
        root.path / "ledger" / "L3" / "research" / "L-9.json"
    )
    assert root.ledger_union_index == root.path / "ledger" / "union.index.json"
    assert root.global_lock_path == root.path / ".ht-global.lock"


def test_union_helpers_scan_books_and_allocate_one_global_namespace(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed_entry(ctx.root, book="L4", section="research", entry_id="L-7")
    _seed_entry(ctx.root, book="top", section="user", entry_id="L-2")

    entries = _common.iter_ledger_entries(ctx)

    assert [(book, section, path.name, doc["id"]) for book, section, path, doc in entries] == [
        ("L4", "research", "L-7.json", "L-7"),
        ("top", "user", "L-2.json", "L-2"),
    ]
    assert _common.find_ledger_entry(ctx, "L-7") == (
        "L4",
        "research",
        {"id": "L-7"},
    )
    assert _common.next_ledger_id(ctx) == "L-8"


def test_find_ledger_entry_rejects_duplicate_id_across_books(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed_entry(ctx.root, book="L3", section="observatory", entry_id="L-4")
    _seed_entry(ctx.root, book="top", section="research", entry_id="L-4")

    with pytest.raises(HtError, match=r"duplicated across the union"):
        _common.find_ledger_entry(ctx, "L-4")


def test_global_mutex_contention_is_bounded_and_clean(tmp_path: Path) -> None:
    root = Root(tmp_path)
    started = time.monotonic()

    with global_mutex(root):
        with pytest.raises(HtError, match=r"global merge/ledger mutex is contended"):
            with global_mutex(root, timeout=0.02, poll_interval=0.001):
                pytest.fail("a contended mutex must not enter its critical section")

    assert time.monotonic() - started < 0.5


def test_validate_flags_empty_orphan_book_but_accepts_registered_lane(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    orphan = ctx.root.ledger_book_dir("typo")
    orphan.mkdir(parents=True)

    assert validate._ledger_layout_errors(ctx) == [
        "ledger/typo: orphan ledger book 'typo' "
        "(expected 'top' or a registered lane with trees/<lane>/tree.json)"
    ]

    (ctx.root.trees_dir / "typo").mkdir(parents=True)
    (ctx.root.tree_json("typo")).write_text("{}\n", encoding="utf-8")

    assert validate._ledger_layout_errors(ctx) == []


def test_d8_phase_b_matcher_uses_normalized_threshold_and_stopwords() -> None:
    assert ledger._is_d8_candidate(
        "The recurring orientation waste in sessions",
        "Orientation waste blocks useful work",
    )
    assert ledger._is_d8_candidate("Cache", "the CACHE is stale")
    assert not ledger._is_d8_candidate(
        "Orientation overhead in sessions",
        "Orientation waste blocks work",
    )
    assert not ledger._is_d8_candidate("the and of", "the and of")


def test_d8_clean_create_has_book_and_lane_provenance(tmp_path: Path) -> None:
    ctx = Ctx(root=Root(tmp_path), role="verifier", lane="L4")

    plan = ledger.create(
        ctx,
        "research",
        "Novel database latency hypothesis",
        "observation#O-1",
        None,
        None,
        book="top",
    )

    assert len(plan.writes) == 1
    write = plan.writes[0]
    assert write.path == ctx.root.ledger_entry("top", "research", "L-1")
    assert write.ledger_book == "top"
    assert write.new["lane"] == "L4"
    assert write.new["dedup_log"] == []


def test_d8_union_screen_surfaces_cross_book_candidate_and_requires_disposition(
    tmp_path: Path,
) -> None:
    ctx = Ctx(root=Root(tmp_path), role="verifier", lane="L4")
    _seed_ledger_doc(
        ctx.root,
        book="L7",
        section="observatory",
        entry_id="L-3",
        text="Recurring orientation waste across sessions",
    )

    with pytest.raises(
        HtError,
        match=r"D8 union screen surfaced candidates: L-3.*nothing.*auto-merges",
    ):
        ledger.create(
            ctx,
            "research",
            "Orientation waste appears repeatedly",
            "observation#O-2",
            None,
            None,
            book="L4",
        )


def test_d8_distinct_disposition_must_exactly_cover_candidates_and_is_logged(
    tmp_path: Path,
) -> None:
    ctx = Ctx(root=Root(tmp_path), role="verifier", lane="L4")
    _seed_ledger_doc(
        ctx.root,
        book="L7",
        section="research",
        entry_id="L-2",
        text="Recurring orientation waste across sessions",
    )
    _seed_ledger_doc(
        ctx.root,
        book="top",
        section="user",
        entry_id="L-5",
        text="Orientation waste blocks agent progress",
    )

    with pytest.raises(HtError, match=r"exactly the surfaced candidates: L-2, L-5"):
        ledger.create(
            ctx,
            "research",
            "Orientation waste appears repeatedly",
            "observation#O-3",
            None,
            None,
            book="L4",
            dedup_distinct=["L-2"],
        )

    plan = ledger.create(
        ctx,
        "research",
        "Orientation waste appears repeatedly",
        "observation#O-3",
        None,
        None,
        book="L4",
        dedup_distinct=["L-5", "L-2"],
    )
    assert plan.writes[0].new["id"] == "L-6"
    assert plan.writes[0].new["dedup_log"] == [
        {"matched": "L-2", "resolution": "distinct"},
        {"matched": "L-5", "resolution": "distinct"},
    ]


def test_d8_abandon_and_echo_uses_proposal_source_ref_atomically(
    tmp_path: Path,
) -> None:
    ctx = Ctx(root=Root(tmp_path), role="verifier", lane="L4")
    old = _seed_ledger_doc(
        ctx.root,
        book="L7",
        section="research",
        entry_id="L-8",
        text="Recurring orientation waste across sessions",
    )

    plan = ledger.create(
        ctx,
        "research",
        "Orientation waste appears repeatedly",
        "observation#O-9",
        None,
        None,
        book="L4",
        abandon_and_echo="L-8",
    )

    assert len(plan.writes) == 1
    write = plan.writes[0]
    assert write.path == ctx.root.ledger_entry("L7", "research", "L-8")
    assert write.old == old
    assert write.new["support_count"] == 1
    assert write.new["echoes"] == [
        {"source_ref": "observation#O-9", "epoch": 0}
    ]
    assert write.new["dedup_log"] == [
        {"matched": "observation#O-9", "resolution": "merged"}
    ]


def test_d8_schema_carries_verbatim_framing_and_phase_b_threshold() -> None:
    schema = json.loads(
        (Path(__file__).parents[1] / "system/schemas/ledger_entry.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert ledger.D8_FRAMING in schema["$comment"]
    assert (
        "candidate iff shared-token intersection >= min(2, len(shorter token set))"
        in schema["properties"]["text"]["$comment"]
    )
    assert schema["properties"]["lane"]["type"] == ["string", "null"]


def test_d8_create_help_carries_verbatim_director_framing(sandbox) -> None:
    result = sandbox.run("ledger", "create", "--help")

    assert result.returncode == 0, result.stderr
    assert ledger.D8_FRAMING in result.stdout


def test_concurrent_cross_book_creates_keep_global_ids_unique(sandbox) -> None:
    for lane in ("L4", "L5"):
        result = sandbox.run(
            "tree",
            "init",
            lane,
            "--root-question",
            f"Question for {lane}?",
            role="director",
        )
        assert result.returncode == 0, result.stderr

    barrier = threading.Barrier(2)

    def create_with_bounded_retry(lane: str, text: str):
        barrier.wait()
        last = None
        for _attempt in range(20):
            last = sandbox.run(
                "ledger",
                "create",
                "--section",
                "research",
                "--book",
                lane,
                "--text",
                text,
                "--proposed-by",
                f"observation#{lane}",
                role="verifier",
                env_extra={"HT_LANE": lane},
            )
            if last.returncode == 0:
                return last
            if "global merge/ledger mutex is contended" not in last.stderr:
                return last
            time.sleep(0.01)
        return last

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: create_with_bounded_retry(*args),
                (("L4", "Unique database latency"), ("L5", "Novel renderer jitter")),
            )
        )

    assert all(result is not None and result.returncode == 0 for result in results), [
        result.stderr if result is not None else "missing result" for result in results
    ]
    ctx = _ctx(sandbox.root)
    entries = _common.iter_ledger_entries(ctx)
    assert {book for book, _section, _path, _doc in entries} == {"L4", "L5"}
    assert {doc["id"] for _book, _section, _path, doc in entries} == {"L-1", "L-2"}
    assert len({doc["id"] for _book, _section, _path, doc in entries}) == 2


def test_unassigned_verifier_rejected_for_create_and_echo(sandbox) -> None:
    rejected_create = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--text",
        "Unassigned create",
        role="verifier",
    )
    assert rejected_create.returncode != 0
    assert "unassigned verifier lane" in rejected_create.stderr

    created = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--text",
        "Assigned target",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert created.returncode == 0, created.stderr
    rejected_echo = sandbox.run(
        "ledger",
        "echo",
        "--entry",
        "L-1",
        "--source-ref",
        "observation#unassigned",
        role="verifier",
    )
    assert rejected_echo.returncode != 0
    assert "unassigned verifier lane" in rejected_echo.stderr


def test_assigned_verifier_own_book_create_and_echo(sandbox) -> None:
    created = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--text",
        "Own book candidate",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert created.returncode == 0, created.stderr

    echoed = sandbox.run(
        "ledger",
        "echo",
        "--entry",
        "L-1",
        "--source-ref",
        "observation#own",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert echoed.returncode == 0, echoed.stderr
    entry = sandbox.load("ledger/L4/research/L-1.json")
    assert entry["lane"] == "L4"
    assert entry["echoes"] == [{"source_ref": "observation#own", "epoch": 0}]


def test_assigned_verifier_top_create_records_lane_and_wrong_book_rejects(sandbox) -> None:
    top = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        "top",
        "--text",
        "Top book candidate",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert top.returncode == 0, top.stderr
    assert sandbox.load("ledger/top/research/L-1.json")["lane"] == "L4"

    wrong = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        "L5",
        "--text",
        "Spectral parser anomaly",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert wrong.returncode != 0
    assert "may not create in ledger book 'L5'" in wrong.stderr


def test_cross_book_echo_requires_and_accepts_atomic_dedup(sandbox) -> None:
    created = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--text",
        "Cross book echo target",
        role="verifier",
        env_extra={"HT_LANE": "L5"},
    )
    assert created.returncode == 0, created.stderr

    non_atomic = sandbox.run(
        "ledger",
        "echo",
        "--entry",
        "L-1",
        "--source-ref",
        "observation#cross",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert non_atomic.returncode != 0
    assert "only via atomic echo + support + dedup append" in non_atomic.stderr

    atomic = sandbox.run(
        "ledger",
        "echo",
        "--entry",
        "L-1",
        "--source-ref",
        "observation#cross",
        "--dedup-matched",
        "observation#cross",
        "--dedup-resolution",
        "merged",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert atomic.returncode == 0, atomic.stderr
    entry = sandbox.load("ledger/L5/research/L-1.json")
    assert entry["support_count"] == 1
    assert entry["echoes"] == [
        {"source_ref": "observation#cross", "epoch": 0}
    ]
    assert entry["dedup_log"] == [
        {"matched": "observation#cross", "resolution": "merged"}
    ]


@pytest.mark.parametrize("book", ["top", "L4"])
def test_user_cannot_create_research_section_in_any_book(sandbox, book: str) -> None:
    result = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        book,
        "--text",
        f"User research create {book}",
        role="user",
    )

    assert result.returncode != 0
    assert "creator: verifier" in result.stderr


def test_union_index_regenerates_on_create_status_and_echo_and_validates(sandbox) -> None:
    created = sandbox.run(
        "ledger", "create", "--section", "user", "--text", "Unique alpha hypothesis",
        role="user",
    )
    assert created.returncode == 0, created.stderr
    union = sandbox.load("ledger/union.index.json")
    assert union["entries"] == [
        {
            "id": "L-1",
            "book": "top",
            "section": "user",
            "path": "ledger/top/user/L-1.json",
            "text": "Unique alpha hypothesis",
            "status": {"state": "open", "ref": None, "reason": None},
        }
    ]

    status = sandbox.run(
        "ledger", "status", "--entry", "L-1", "--to", "retired",
        "--reason", "triaged", role="director",
    )
    assert status.returncode == 0, status.stderr
    assert sandbox.load("ledger/union.index.json")["entries"][0]["status"] == {
        "state": "retired", "ref": None, "reason": "triaged"
    }

    assert sandbox.run(
        "tree", "init", "L4", "--root-question", "Does L4 hold?", role="director"
    ).returncode == 0
    verifier_create = sandbox.run(
        "ledger", "create", "--section", "research",
        "--text", "Quantum basalt orchard", "--proposed-by", "observation#O-1",
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert verifier_create.returncode == 0, verifier_create.stderr
    union_path = sandbox.root / "ledger/union.index.json"
    before_echo_mtime = union_path.stat().st_mtime_ns
    time.sleep(0.01)
    echoed = sandbox.run(
        "ledger", "echo", "--entry", "L-2", "--source-ref", "observation#O-2",
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert echoed.returncode == 0, echoed.stderr
    # Echo support is intentionally absent from the compact union projection, so
    # Git may have no content delta; mtime proves the wholesale regeneration ran.
    assert union_path.stat().st_mtime_ns > before_echo_mtime
    assert sandbox.run("validate", "--all").returncode == 0

    sandbox.write_file(
        "ledger/union.index.json",
        json.dumps({"generated": True, "citable": False, "entries": []}) + "\n",
    )
    stale = sandbox.run("validate", "--all")
    assert stale.returncode != 0
    assert "stale or inconsistent generated ledger union index" in stale.stderr


@pytest.mark.parametrize("lane", [None, "L5"])
def test_hook_rejects_ledger_deletion_for_unassigned_or_wrong_lane(sandbox, lane):
    assert sandbox.run(
        "tree", "init", "L4", "--root-question", "Does L4 hold?", role="director"
    ).returncode == 0
    created = sandbox.run(
        "ledger", "create", "--section", "research", "--text", "Durable ledger record",
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert created.returncode == 0, created.stderr
    rel = "ledger/L4/research/L-1.json"
    assert sandbox.git("rm", rel).returncode == 0
    env = {"HT_COMMIT": "1", "HT_ROLE": "verifier"}
    if lane is not None:
        env["HT_LANE"] = lane
    rejected = sandbox.git("commit", "-m", "delete ledger", env_extra=env)
    assert rejected.returncode != 0
    assert "removes or renames a ledger entry" in rejected.stderr
    assert sandbox.git("reset", "--hard", "HEAD").returncode == 0
    assert (sandbox.root / rel).exists()


def test_hook_rejects_ledger_rename_and_union_index_deletion(sandbox) -> None:
    created = sandbox.run(
        "ledger", "create", "--section", "user", "--text", "Durable user record",
        role="user",
    )
    assert created.returncode == 0, created.stderr
    rel = "ledger/top/user/L-1.json"
    assert sandbox.git("mv", rel, "escaped-L-1.json").returncode == 0
    renamed = sandbox.git(
        "commit", "-m", "rename ledger",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "user"},
    )
    assert renamed.returncode != 0
    assert "removes or renames a ledger entry" in renamed.stderr
    assert sandbox.git("reset", "--hard", "HEAD").returncode == 0

    assert sandbox.git("rm", "ledger/union.index.json").returncode == 0
    deleted_union = sandbox.git(
        "commit", "-m", "delete union",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert deleted_union.returncode != 0
    assert "generated ledger union index" in deleted_union.stderr


def test_validate_rejects_path_id_mismatch_and_duplicate_union_ids(sandbox) -> None:
    for lane in ("L4", "L5"):
        assert sandbox.run(
            "tree", "init", lane, "--root-question", f"Does {lane} hold?",
            role="director",
        ).returncode == 0
    created = sandbox.run(
        "ledger", "create", "--section", "research", "--text", "Canonical record",
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert created.returncode == 0, created.stderr

    root = Root(sandbox.root)
    duplicate = _seed_ledger_doc(
        root, book="L5", section="research", entry_id="L-1", text="Duplicate record"
    )
    mismatch_path = root.ledger_entry("L5", "observatory", "L-2")
    mismatch_path.parent.mkdir(parents=True, exist_ok=True)
    mismatch_path.write_text(
        json.dumps({**duplicate, "id": "L-99", "section": "observatory"}) + "\n",
        encoding="utf-8",
    )
    ledger_index.regenerate(root)

    invalid = sandbox.run("validate", "--all")
    assert invalid.returncode != 0
    assert "duplicate union-global ledger id 'L-1'" in invalid.stderr
    assert "ledger id 'L-99' does not match filename 'L-2'" in invalid.stderr


def test_hook_rejects_duplicate_global_id_across_books(sandbox) -> None:
    for lane in ("L4", "L5"):
        assert sandbox.run(
            "tree", "init", lane, "--root-question", f"Does {lane} hold?",
            role="director",
        ).returncode == 0
    created = sandbox.run(
        "ledger", "create", "--section", "research", "--text", "Canonical record",
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert created.returncode == 0, created.stderr
    root = Root(sandbox.root)
    duplicate_path = root.ledger_entry("L5", "research", "L-1")
    duplicate = sandbox.load("ledger/L4/research/L-1.json")
    duplicate_path.parent.mkdir(parents=True, exist_ok=True)
    duplicate_path.write_text(
        json.dumps({**duplicate, "text": "Duplicate", "lane": "L5"}) + "\n",
        encoding="utf-8",
    )
    ledger_index.regenerate(root)
    assert sandbox.git(
        "add", "ledger/L5/research/L-1.json", "ledger/union.index.json"
    ).returncode == 0
    rejected = sandbox.git(
        "commit", "-m", "duplicate global id",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "verifier", "HT_LANE": "L5"},
    )
    assert rejected.returncode != 0
    assert "duplicates union-global ledger id 'L-1'" in rejected.stderr


@pytest.mark.parametrize("role", ["harness", "director", "unit", "pc", "cgate"])
def test_non_verifier_roles_cannot_create_research_entries(sandbox, role: str) -> None:
    rejected = sandbox.run(
        "ledger", "create", "--section", "research", "--text", "Wrong role",
        role=role,
    )
    assert rejected.returncode != 0
    assert "creator: verifier" in rejected.stderr
