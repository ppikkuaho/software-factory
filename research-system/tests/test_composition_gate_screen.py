"""Item-7 W2 tests for the committed-state mechanical screen engine."""

from __future__ import annotations

import json
from pathlib import Path
import unicodedata

import pytest

from composition_gate.cli import main as cgate_main
from composition_gate.config import load_config
from composition_gate.screen import CHECK_NAMES, render_screen, run_screen

from conftest import Sandbox, seed_mrec_candidate


def _scope(
    lane: str,
    *,
    seats: list[str] | None = None,
    surfaces: list[str] | None = None,
    globs: list[str] | None = None,
) -> dict:
    return {
        "lane": lane,
        "seats": seats or [],
        "surfaces": surfaces or [],
        "globs": globs or [],
    }


def _record(
    record_id: str,
    *,
    lane: str,
    node: str | None = None,
    scope: dict | None = None,
    consumed_epoch: int | None = None,
    gate_verdict: dict | None = None,
) -> dict:
    return {
        "id": record_id,
        "candidate_ref": f"tree#{lane}/node#{node or record_id.lower()}",
        "lane_verdict": "lane-pass",
        "scope": scope or _scope(lane),
        "screen": {"results": [], "log_ref": None},
        "gate_verdict": gate_verdict,
        "watch_link": None,
        "created": "2026-07-13",
        "consumed_epoch": consumed_epoch,
    }


def _tree(component: str, rows: list[dict]) -> dict:
    return {
        "component": component,
        "root_question": f"Synthetic question for {component}",
        "epoch": 0,
        "epoch_history": [],
        "cursor": [],
        "decision_log": [],
        "global_learnings": [],
        "watch_queue": rows,
    }


def _commit_documents(sb: Sandbox, documents: dict[str, dict | str], message: str) -> None:
    for rel, document in documents.items():
        content = document if isinstance(document, str) else json.dumps(document, indent=2) + "\n"
        sb.write_file(rel, content)
    added = sb.git("add", "--", *documents)
    assert added.returncode == 0, added.stderr
    committed = sb.git("commit", "--no-verify", "-m", message)
    assert committed.returncode == 0, committed.stderr


def _record_path(record_id: str) -> str:
    return f"tier1/merge-records/{record_id}.json"


def _result(output: dict, check: str) -> dict:
    return next(row for row in output["results"] if row["check"] == check)


def _assert_citations(output: dict) -> None:
    assert [row["check"] for row in output["results"]] == list(CHECK_NAMES)
    assert output["config_hash"] == load_config().sha256
    assert isinstance(output["head_commit"], str)
    assert isinstance(output["head_tree"], str)
    store_map = {
        "scope-overlap": {"tier1/merge-records"},
        "surface-budget": {"tier1/merge-records"},
        "settlement-completeness": {"tier1/merge-records", "trees"},
        "queue-adjacency": {"tier1/merge-records"},
        "watch-debt": {"tier1/merge-records", "trees"},
    }
    for row in output["results"]:
        assert isinstance(row["inputs"], dict)
        assert row["inputs"]["config"] == {
            "name": "screen-config.v1.json",
            "sha256": output["config_hash"],
        }
        discovery = row["inputs"]["discovery"]
        assert discovery["snapshot"] == {
            "status": "ok",
            "head_commit": output["head_commit"],
            "head_tree": output["head_tree"],
            "error": None,
        }
        assert set(discovery["stores"]) == store_map[row["check"]]
        assert all(store["status"] == "ok" for store in discovery["stores"].values())


def test_baseline_results_cover_pass_and_honest_na_with_exact_citations(
    sandbox: Sandbox,
):
    candidate = _record("MR-1", lane="candidate")
    _commit_documents(sandbox, {_record_path("MR-1"): candidate}, "seed candidate")

    output = run_screen(sandbox.root, "MR-1")

    assert {row["check"]: row["result"] for row in output["results"]} == {
        "scope-overlap": "pass",
        "surface-budget": "n/a",
        "settlement-completeness": "pass",
        "queue-adjacency": "pass",
        "watch-debt": "n/a",
    }
    assert _result(output, "surface-budget")["detail"] == (
        "no actor-visible surfaces declared"
    )
    assert _result(output, "watch-debt")["detail"] == (
        "no watch outcomes exist in v1"
    )
    _assert_citations(output)


def test_empty_surface_candidate_still_preflights_malformed_record_sibling(
    sandbox: Sandbox,
):
    _commit_documents(
        sandbox,
        {
            _record_path("MR-1"): _record("MR-1", lane="candidate"),
            _record_path("MR-2"): "{malformed sibling\n",
        },
        "seed empty candidate and malformed sibling",
    )

    output = run_screen(sandbox.root, "MR-1")

    for row in output["results"]:
        assert row["result"] == "fail"
        assert "unreadable JSON at tier1/merge-records/MR-2.json" in row["detail"]
    assert _result(output, "surface-budget")["result"] != "n/a"


def test_discovery_citations_survive_mrec_screen_transcription(sandbox: Sandbox):
    _commit_documents(
        sandbox,
        {_record_path("MR-1"): _record("MR-1", lane="candidate")},
        "seed candidate",
    )
    output = run_screen(sandbox.root, "MR-1")
    payload = sandbox.root / "var/MR-1-real-screen.json"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(render_screen(output), encoding="utf-8")

    result = sandbox.run(
        "mrec",
        "screen",
        "MR-1",
        "--results-json",
        str(payload),
        "--log-ref",
        "var/MR-1-real-screen.json",
        role="harness",
    )

    assert result.returncode == 0, result.stderr
    stored = sandbox.load(_record_path("MR-1"))["screen"]["results"]
    assert stored == output["results"]
    for row in stored:
        assert row["inputs"]["discovery"]["snapshot"]["head_commit"] == output["head_commit"]


@pytest.mark.parametrize(
    ("marker", "content", "failing_checks"),
    [
        (
            "tier1/merge-records/.gitkeep",
            json.dumps(_record("MR-2", lane="hidden")),
            CHECK_NAMES,
        ),
        (
            "trees/.gitkeep",
            json.dumps(_tree("hidden", [])),
            ("settlement-completeness", "watch-debt"),
        ),
    ],
)
def test_sanctioned_markers_must_be_empty_bytes(
    sandbox: Sandbox,
    marker: str,
    content: str,
    failing_checks: tuple[str, ...],
):
    candidate = _record(
        "MR-1", lane="candidate", scope=_scope("candidate", surfaces=["panel"])
    )
    _commit_documents(
        sandbox,
        {_record_path("MR-1"): candidate, marker: content},
        "seed non-empty sanctioned marker",
    )

    output = run_screen(sandbox.root, "MR-1")
    for check in failing_checks:
        row = _result(output, check)
        assert row["result"] == "fail"
        assert "must be empty bytes" in row["detail"]


@pytest.mark.parametrize("invalid_path", ["mr-2.json", "MR-2.JSON"])
def test_case_variant_merge_record_filename_fails_every_record_consumer(
    sandbox: Sandbox,
    invalid_path: str,
):
    candidate = _record(
        "MR-1",
        lane="candidate",
        scope=_scope("candidate", surfaces=["panel"]),
    )
    stray = _record("MR-2", lane="other")
    _commit_documents(
        sandbox,
        {
            _record_path("MR-1"): candidate,
            f"tier1/merge-records/{invalid_path}": stray,
        },
        "seed case-variant merge-record path",
    )

    output = run_screen(sandbox.root, "MR-1")
    for check in CHECK_NAMES:
        row = _result(output, check)
        assert row["result"] == "fail"
        assert "screen-error" in row["detail"]
        assert "unexpected committed merge-record path" in row["detail"]


def test_casefold_duplicate_merge_record_paths_fail_closed(sandbox: Sandbox):
    candidate = _record(
        "MR-1", lane="candidate", scope=_scope("candidate", surfaces=["panel"])
    )
    duplicate = _record("MR-2", lane="other")
    _commit_documents(
        sandbox,
        {_record_path("MR-1"): candidate, _record_path("MR-2"): duplicate},
        "seed canonical records",
    )
    blob = sandbox.git("rev-parse", "HEAD:tier1/merge-records/MR-2.json")
    assert blob.returncode == 0, blob.stderr
    aliased = sandbox.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob.stdout.strip()},tier1/merge-records/mr-2.json",
    )
    assert aliased.returncode == 0, aliased.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "add casefold alias")
    assert committed.returncode == 0, committed.stderr

    row = _result(run_screen(sandbox.root, "MR-1"), "scope-overlap")
    assert row["result"] == "fail"
    assert "case-fold duplicate committed paths" in row["detail"]


@pytest.mark.parametrize("remove_form", ["NFC", "NFD"])
def test_nfc_nfd_tree_twins_and_twin_deletion_never_silence_watch_debt(
    sandbox: Sandbox,
    remove_form: str,
):
    configured = sandbox.git("config", "core.precomposeunicode", "false")
    assert configured.returncode == 0, configured.stderr
    configured = sandbox.git("config", "core.quotePath", "false")
    assert configured.returncode == 0, configured.stderr

    candidate = _record(
        "MR-1", lane="candidate", scope=_scope("candidate", surfaces=["panel"])
    )
    source = _record(
        "MR-2",
        lane="source",
        node="merged",
        scope=_scope("source", surfaces=["panel"]),
        consumed_epoch=1,
        gate_verdict={"verdict": "land", "date": "2026-07-13"},
    )
    records: dict[str, dict] = {
        _record_path("MR-1"): candidate,
        _record_path("MR-2"): source,
    }
    for epoch, ordinal in enumerate(range(3, 8), start=2):
        record_id = f"MR-{ordinal}"
        records[_record_path(record_id)] = _record(
            record_id,
            lane=f"new-{ordinal}",
            scope=_scope(f"new-{ordinal}", surfaces=["other"]),
            consumed_epoch=epoch,
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        )
    _commit_documents(sandbox, records, "seed watched source outside K window")

    watch = {
        "id": "W-1",
        "merged_node": source["candidate_ref"],
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": 1,
    }
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    paths = {
        "NFC": f"trees/{nfc}/tree.json",
        "NFD": f"trees/{nfd}/tree.json",
    }
    for label, rows in (("NFC", [watch]), ("NFD", [])):
        blob_file = sandbox.write_file(
            f"var/{label}-tree.json",
            json.dumps(_tree(nfc, rows), ensure_ascii=False, indent=2) + "\n",
        )
        blob = sandbox.git("hash-object", "-w", str(blob_file))
        assert blob.returncode == 0, blob.stderr
        staged = sandbox.git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob.stdout.strip()},{paths[label]}",
        )
        assert staged.returncode == 0, staged.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "seed NFC NFD twins")
    assert committed.returncode == 0, committed.stderr

    before = run_screen(sandbox.root, "MR-1")
    for check in ("settlement-completeness", "watch-debt"):
        row = _result(before, check)
        assert row["result"] == "fail"
        assert "NFC+case-fold duplicate committed paths" in row["detail"]

    removed = sandbox.git("update-index", "--force-remove", "--", paths[remove_form])
    assert removed.returncode == 0, removed.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", f"delete {remove_form} twin")
    assert committed.returncode == 0, committed.stderr

    after_watch = _result(run_screen(sandbox.root, "MR-1"), "watch-debt")
    assert after_watch["result"] == "fail"
    assert after_watch["result"] != "n/a"
    if remove_form == "NFC":
        assert "non-NFC committed path" in after_watch["detail"]
    else:
        assert after_watch["detail"].startswith("open disconfirmation watch debt")


def test_ht_created_unicode_tree_screens_clean_with_default_git_quoting(
    sandbox: Sandbox,
):
    adjudication_ref = seed_mrec_candidate(sandbox, "tree#candidate/node#1")
    created = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#candidate/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "candidate",
        role="harness",
    )
    assert created.returncode == 0, created.stderr
    unicode_name = unicodedata.normalize("NFC", "café")
    initialized = sandbox.run(
        "tree",
        "init",
        unicode_name,
        "--root-question",
        "Unicode tree",
        role="director",
    )
    assert initialized.returncode == 0, initialized.stderr

    output = run_screen(sandbox.root, "MR-1")
    assert not any(row["detail"].startswith("screen-error:") for row in output["results"])


def test_deleted_tree_json_fails_tree_consuming_checks(sandbox: Sandbox):
    _commit_documents(
        sandbox,
        {
            _record_path("MR-1"): _record("MR-1", lane="candidate"),
            "trees/observer/tree.json": _tree("observer", []),
            "trees/observer/marker.txt": "tree remains logically present\n",
        },
        "seed tree directory",
    )
    removed = sandbox.git("rm", "trees/observer/tree.json")
    assert removed.returncode == 0, removed.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "delete tree json")
    assert committed.returncode == 0, committed.stderr

    output = run_screen(sandbox.root, "MR-1")
    for check in ("settlement-completeness", "watch-debt"):
        row = _result(output, check)
        assert row["result"] == "fail"
        assert "missing exact tree.json" in row["detail"]


def test_deleting_tree_cannot_flip_live_watch_debt_flag_to_silent_na(
    sandbox: Sandbox,
):
    candidate = _record(
        "MR-1",
        lane="candidate",
        scope=_scope("candidate", surfaces=["panel"]),
    )
    source = _record(
        "MR-2",
        lane="source",
        node="merged",
        scope=_scope("source", surfaces=["panel"]),
        consumed_epoch=1,
        gate_verdict={"verdict": "land", "date": "2026-07-13"},
    )
    documents: dict[str, dict | str] = {
        _record_path("MR-1"): candidate,
        _record_path("MR-2"): source,
        "trees/observer/marker.txt": "component still has committed content\n",
    }
    for epoch, ordinal in enumerate(range(3, 8), start=2):
        record_id = f"MR-{ordinal}"
        documents[_record_path(record_id)] = _record(
            record_id,
            lane=f"new-{ordinal}",
            scope=_scope(f"new-{ordinal}", surfaces=["other"]),
            consumed_epoch=epoch,
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        )
    watch = {
        "id": "W-1",
        "merged_node": source["candidate_ref"],
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": 1,
    }
    documents["trees/observer/tree.json"] = _tree("observer", [watch])
    _commit_documents(sandbox, documents, "seed watched source outside K window")

    before = run_screen(sandbox.root, "MR-1")
    before_watch = _result(before, "watch-debt")
    assert before_watch["result"] == "fail"
    assert before_watch["detail"].startswith("open disconfirmation watch debt")
    assert not any(row["detail"].startswith("screen-error:") for row in before["results"])
    selected = {
        row["record_id"]: row["selected"]
        for row in _result(before, "scope-overlap")["inputs"]["consumed_inventory"]
    }
    assert selected["MR-2"] is False

    removed = sandbox.git("rm", "trees/observer/tree.json")
    assert removed.returncode == 0, removed.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "delete required tree json")
    assert committed.returncode == 0, committed.stderr

    after = run_screen(sandbox.root, "MR-1")
    for check in ("settlement-completeness", "watch-debt"):
        row = _result(after, check)
        assert row["result"] == "fail"
        assert row["detail"].startswith("screen-error:")
        assert "missing exact tree.json" in row["detail"]
    assert _result(after, "watch-debt")["result"] != "n/a"


def test_case_variant_tree_json_fails_tree_consuming_checks(sandbox: Sandbox):
    _commit_documents(
        sandbox,
        {
            _record_path("MR-1"): _record("MR-1", lane="candidate"),
            "trees/observer/Tree.json": _tree("observer", []),
        },
        "seed case-variant tree path",
    )

    output = run_screen(sandbox.root, "MR-1")
    for check in ("settlement-completeness", "watch-debt"):
        row = _result(output, check)
        assert row["result"] == "fail"
        assert "case-variant tree.json path" in row["detail"]


def test_scope_overlap_uses_last_five_pending_classes_and_glob_matching_both_ways(
    sandbox: Sandbox,
):
    documents: dict[str, dict] = {}
    for epoch in range(1, 7):
        scope = _scope(
            f"consumed-{epoch}",
            seats=["candidate-seat"] if epoch == 1 else [],
        )
        record_id = f"MR-{epoch}"
        documents[_record_path(record_id)] = _record(
            record_id,
            lane=f"consumed-{epoch}",
            scope=scope,
            consumed_epoch=epoch,
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        )
    documents[_record_path("MR-10")] = _record(
        "MR-10",
        lane="candidate",
        scope=_scope(
            "candidate",
            seats=["candidate-seat"],
            globs=["src/pkg/file.py", "tests/*.py"],
        ),
    )
    _commit_documents(sandbox, documents, "seed K-window records")

    windowed = run_screen(sandbox.root, "MR-10")
    overlap = _result(windowed, "scope-overlap")
    assert overlap["result"] == "pass"
    inventory = overlap["inputs"]["consumed_inventory"]
    assert [row["record_id"] for row in inventory] == [
        "MR-6",
        "MR-5",
        "MR-4",
        "MR-3",
        "MR-2",
        "MR-1",
    ]
    assert [row["selected"] for row in inventory] == [True, True, True, True, True, False]

    pending = {
        _record_path("MR-8"): _record(
            "MR-8",
            lane="pending-null",
            scope=_scope("pending-null", globs=["src/**/*.py"]),
        ),
        _record_path("MR-9"): _record(
            "MR-9",
            lane="pending-land",
            scope=_scope("pending-land", globs=["tests/test_gate.py"]),
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        ),
        _record_path("MR-11"): _record(
            "MR-11",
            lane="not-pending",
            scope=_scope("not-pending", seats=["candidate-seat"]),
            gate_verdict={"verdict": "hold", "date": "2026-07-13"},
        ),
    }
    _commit_documents(sandbox, pending, "seed pending definitions")

    output = run_screen(sandbox.root, "MR-10")
    overlap = _result(output, "scope-overlap")
    assert overlap["result"] == "fail"
    assert overlap["inputs"]["pending_record_ids"] == ["MR-8", "MR-9"]
    collisions = {row["record_id"]: row for row in overlap["inputs"]["collisions"]}
    assert collisions["MR-8"]["values"]["globs"] == [
        {
            "candidate_glob": "src/pkg/file.py",
            "other_glob": "src/**/*.py",
            "mode": "candidate-matched-by-other",
        }
    ]
    assert collisions["MR-9"]["values"]["globs"] == [
        {
            "candidate_glob": "tests/*.py",
            "other_glob": "tests/test_gate.py",
            "mode": "other-matched-by-candidate",
        }
    ]
    assert "MR-11" not in collisions
    _assert_citations(output)


def test_scope_aliases_casefold_and_normalize_before_collision_checks(
    sandbox: Sandbox,
):
    candidate = _record(
        "MR-1",
        lane="CAFÉ",
        scope=_scope(
            "CAFÉ",
            seats=["SÉNIOR"],
            surfaces=["PANEL"],
            globs=["CAFÉ/Report.py"],
        ),
    )
    other = _record(
        "MR-2",
        lane="café",
        scope=_scope(
            "café",
            seats=["sénior"],
            surfaces=["panel"],
            globs=["café/*.PY"],
        ),
    )
    _commit_documents(
        sandbox,
        {_record_path("MR-1"): candidate, _record_path("MR-2"): other},
        "seed normalized aliases",
    )

    overlap = _result(run_screen(sandbox.root, "MR-1"), "scope-overlap")
    assert overlap["result"] == "fail"
    collision = overlap["inputs"]["collisions"][0]
    assert collision["axes"] == ["lane", "seats", "surfaces", "globs"]
    assert collision["values"]["globs"][0]["mode"] == "candidate-matched-by-other"


def test_surface_budget_passes_then_fails_on_cumulative_consumed_count(
    sandbox: Sandbox,
):
    candidate = _record(
        "MR-20", lane="candidate", scope=_scope("candidate", surfaces=["merge-queue"])
    )
    initial = {_record_path("MR-20"): candidate}
    for ordinal in range(1, 3):
        record_id = f"MR-{ordinal}"
        initial[_record_path(record_id)] = _record(
            record_id,
            lane=f"lane-{ordinal}",
            scope=_scope(f"lane-{ordinal}", surfaces=["merge-queue"]),
            consumed_epoch=ordinal,
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        )
    _commit_documents(sandbox, initial, "seed under-budget surface history")

    under = _result(run_screen(sandbox.root, "MR-20"), "surface-budget")
    assert under["result"] == "pass"
    assert under["inputs"]["surface_counts"]["merge-queue"]["count"] == 2
    assert under["inputs"]["not_computable"] == {
        "diff_lines": "merge-record v1 does not carry candidate diff size"
    }

    additional: dict[str, dict] = {}
    for ordinal in range(3, 12):
        record_id = f"MR-{ordinal}"
        additional[_record_path(record_id)] = _record(
            record_id,
            lane=f"lane-{ordinal}",
            scope=_scope(f"lane-{ordinal}", surfaces=["merge-queue"]),
            consumed_epoch=ordinal,
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        )
    _commit_documents(sandbox, additional, "seed over-budget surface history")

    over = _result(run_screen(sandbox.root, "MR-20"), "surface-budget")
    assert over["result"] == "fail"
    assert over["inputs"]["thresholds"] == {
        "surface_budget_max_cumulative_directives": 10,
        "surface_budget_max_diff_lines": 500,
    }
    assert over["inputs"]["surface_counts"]["merge-queue"]["count"] == 11


def test_settlement_completeness_flags_only_rows_older_than_lane_frontier(
    sandbox: Sandbox,
):
    source_old = _record(
        "MR-1",
        lane="other",
        node="old",
        consumed_epoch=4,
        gate_verdict={"verdict": "land", "date": "2026-07-13"},
    )
    source_frontier = _record(
        "MR-2",
        lane="candidate",
        node="frontier",
        consumed_epoch=5,
        gate_verdict={"verdict": "land", "date": "2026-07-13"},
    )
    candidate = _record("MR-3", lane="candidate", node="new")
    rows = [
        {
            "id": "W-1",
            "merged_node": source_old["candidate_ref"],
            "prediction_claim": None,
            "observed": None,
            "verdict": None,
            "severity": None,
            "status": "queued",
            "kind": "staleness-assessment",
            "epoch": 4,
        },
        {
            "id": "W-2",
            "merged_node": source_frontier["candidate_ref"],
            "prediction_claim": None,
            "observed": None,
            "verdict": None,
            "severity": None,
            "status": "queued",
            "kind": "staleness-assessment",
            "epoch": 5,
        },
    ]
    _commit_documents(
        sandbox,
        {
            _record_path("MR-1"): source_old,
            _record_path("MR-2"): source_frontier,
            _record_path("MR-3"): candidate,
            "trees/observer/tree.json": _tree("observer", rows),
        },
        "seed settlement rows",
    )

    failed = _result(run_screen(sandbox.root, "MR-3"), "settlement-completeness")
    assert failed["result"] == "fail"
    assert failed["inputs"]["lane_frontier"] == 5
    assert [row["row_id"] for row in failed["inputs"]["overdue_rows"]] == ["W-1"]

    rows[0]["status"] = "resolved"
    _commit_documents(
        sandbox,
        {"trees/observer/tree.json": _tree("observer", rows)},
        "resolve old settlement row",
    )
    passed = _result(run_screen(sandbox.root, "MR-3"), "settlement-completeness")
    assert passed["result"] == "pass"
    assert [row["row_id"] for row in passed["inputs"]["queued_rows"]] == ["W-2"]


def test_queue_adjacency_counts_verdict_null_and_land_unconsumed_only(
    sandbox: Sandbox,
):
    documents = {
        _record_path("MR-1"): _record(
            "MR-1", lane="candidate", scope=_scope("candidate", surfaces=["shared"])
        ),
        _record_path("MR-2"): _record(
            "MR-2", lane="pending-null", scope=_scope("pending-null", surfaces=["shared"])
        ),
        _record_path("MR-3"): _record(
            "MR-3",
            lane="pending-land",
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        ),
        _record_path("MR-4"): _record(
            "MR-4",
            lane="held",
            gate_verdict={"verdict": "hold", "date": "2026-07-13"},
        ),
    }
    _commit_documents(sandbox, documents, "seed adjacency queue")

    adjacency = _result(run_screen(sandbox.root, "MR-1"), "queue-adjacency")
    assert adjacency["result"] == "fail"
    assert adjacency["inputs"]["pending_count"] == 3
    assert [row["record_id"] for row in adjacency["inputs"]["pending_records"]] == [
        "MR-1",
        "MR-2",
        "MR-3",
    ]
    assert adjacency["inputs"]["shared_surfaces"] == [
        {"record_id": "MR-2", "surfaces": ["shared"]}
    ]
    assert "MR-2=shared" in adjacency["detail"]


def test_watch_debt_resolves_queued_row_to_consumed_record_and_overlaps_scope(
    sandbox: Sandbox,
):
    source = _record(
        "MR-1",
        lane="source",
        node="merged",
        scope=_scope("source", surfaces=["control-panel"], globs=["system/*.py"]),
        consumed_epoch=3,
        gate_verdict={"verdict": "land", "date": "2026-07-13"},
    )
    overlapping = _record(
        "MR-2",
        lane="candidate-a",
        scope=_scope(
            "candidate-a", surfaces=["control-panel"], globs=["system/cli.py"]
        ),
    )
    disjoint = _record(
        "MR-3",
        lane="candidate-b",
        scope=_scope("candidate-b", surfaces=["report"], globs=["tests/*.py"]),
    )
    watch = {
        "id": "W-1",
        "merged_node": source["candidate_ref"],
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": 3,
    }
    _commit_documents(
        sandbox,
        {
            _record_path("MR-1"): source,
            _record_path("MR-2"): overlapping,
            _record_path("MR-3"): disjoint,
            "trees/observer/tree.json": _tree("observer", [watch]),
        },
        "seed watch debt",
    )

    failed = _result(run_screen(sandbox.root, "MR-2"), "watch-debt")
    assert failed["result"] == "fail"
    checked = failed["inputs"]["queued_watch_rows"]
    assert checked[0]["source_record"]["record_id"] == "MR-1"
    assert checked[0]["overlap"]["axes"] == ["surfaces", "globs"]

    honest_na = _result(run_screen(sandbox.root, "MR-3"), "watch-debt")
    assert honest_na["result"] == "n/a"
    assert honest_na["detail"] == "no watch outcomes exist in v1"
    assert honest_na["inputs"]["overlapping_watch_rows"] == []


def test_tree_and_watch_rows_use_normalized_stable_ordering(sandbox: Sandbox):
    row_b = {
        "id": "W-B",
        "merged_node": "tree#source/node#b",
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": 1,
    }
    row_a = {**row_b, "id": "W-A", "merged_node": "tree#source/node#a"}
    _commit_documents(
        sandbox,
        {
            _record_path("MR-1"): _record("MR-1", lane="candidate"),
            "trees/B/tree.json": _tree("B", [row_b]),
            "trees/a/tree.json": _tree("a", [row_a]),
        },
        "seed normalized ordering fixtures",
    )

    row = _result(run_screen(sandbox.root, "MR-1"), "settlement-completeness")

    assert [item["tree_path"] for item in row["inputs"]["queued_rows"]] == [
        "trees/a/tree.json",
        "trees/B/tree.json",
    ]


def test_fail_toward_review_converts_missing_corrupt_and_config_errors(
    sandbox: Sandbox,
    tmp_path: Path,
):
    missing = run_screen(sandbox.root, "MR-999")
    assert all(row["result"] == "fail" for row in missing["results"])
    assert all(row["detail"].startswith("screen-error:") for row in missing["results"])

    _commit_documents(
        sandbox,
        {_record_path("MR-1"): "{not-json\n"},
        "seed corrupt candidate",
    )
    corrupt = run_screen(sandbox.root, "MR-1")
    assert all(row["result"] == "fail" for row in corrupt["results"])
    assert all("unreadable JSON" in row["detail"] for row in corrupt["results"])

    missing_config = run_screen(
        sandbox.root,
        "MR-1",
        config_path=tmp_path / "missing-screen-config.json",
    )
    assert missing_config["config_hash"] == "unavailable"
    assert all(row["result"] == "fail" for row in missing_config["results"])
    assert all(
        "screen-error: ScreenInputError: cannot load config "
        "missing-screen-config.json: No such file or directory" in row["detail"]
        for row in missing_config["results"]
    )
    assert str(tmp_path) not in render_screen(missing_config)
    assert not all(row["result"] == "pass" for row in missing_config["results"])


def test_engine_reads_committed_state_only_and_serialization_is_byte_deterministic(
    sandbox: Sandbox,
):
    candidate = _record("MR-1", lane="candidate")
    rel = _record_path("MR-1")
    _commit_documents(sandbox, {rel: candidate}, "seed deterministic candidate")

    first = render_screen(run_screen(sandbox.root, "MR-1"))
    (sandbox.root / rel).write_text("{dirty-uncommitted-json\n", encoding="utf-8")
    second = render_screen(run_screen(sandbox.root, "MR-1"))
    assert first == second


def test_config_location_does_not_change_output_bytes(
    sandbox: Sandbox,
    tmp_path: Path,
):
    candidate = _record("MR-1", lane="candidate")
    _commit_documents(sandbox, {_record_path("MR-1"): candidate}, "seed candidate")
    raw = load_config().path.read_bytes()
    first_config = tmp_path / "install-a/screen-config.v1.json"
    second_config = tmp_path / "install-b/screen-config.v1.json"
    first_config.parent.mkdir(parents=True)
    second_config.parent.mkdir(parents=True)
    first_config.write_bytes(raw)
    second_config.write_bytes(raw)

    first = render_screen(run_screen(sandbox.root, "MR-1", config_path=first_config))
    second = render_screen(run_screen(sandbox.root, "MR-1", config_path=second_config))
    assert first == second


def test_ht_cgate_screen_writes_identical_stdout_and_var_log(
    sandbox: Sandbox,
    capsys,
):
    candidate = _record("MR-1", lane="candidate")
    _commit_documents(sandbox, {_record_path("MR-1"): candidate}, "seed CLI candidate")

    exit_code = cgate_main(
        [
            "screen",
            "MR-1",
            "--root",
            str(sandbox.root),
            "--out",
            "var/screens/MR-1.json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == (sandbox.root / "var/screens/MR-1.json").read_text()
    assert json.loads(captured.out)["record_id"] == "MR-1"
