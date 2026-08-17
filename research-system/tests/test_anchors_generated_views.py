"""R-i6-3 generated-view anchor rejection and normalization coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import Sandbox, seed_worked_node
from ht import anchors, classify
from ht.errors import HtError
from ht.paths import Root


def _anchor(path: str, start: int = 1, end: int = 1) -> dict:
    return {"path": path, "start_line": start, "end_line": end}


def _assert_banned(root: Root, raw_path: str, normalized: str | None = None) -> None:
    with pytest.raises(HtError) as rejected:
        anchors.resolve(root, _anchor(raw_path))

    message = str(rejected.value)
    assert raw_path in message
    assert (normalized or raw_path) in message
    assert "[R-i6-3] mechanical path-class ban" in message


def _prepare_grant(sb: Sandbox) -> str:
    seed_worked_node(sb)
    dispatch_id = "d-1-2"
    created = sb.run(
        "dispatch",
        "create",
        "--node",
        "1",
        "--question",
        "Can this claim be granted?",
        "--done-definition",
        "Resolve the supplied anchor",
        role="director",
    )
    assert created.returncode == 0, created.stderr
    source = sb.write_file("second-report.md", "one\ntwo\nthree\n")
    submitted = sb.run(
        "report", "submit", "--dispatch", dispatch_id, "--src", str(source), role="unit"
    )
    assert submitted.returncode == 0, submitted.stderr
    return dispatch_id


def _grant(sb: Sandbox, dispatch_id: str, anchor_path: str):
    return sb.run(
        "claim",
        "grant",
        "--dispatch",
        dispatch_id,
        "--text",
        "A claim with cited support",
        "--proposed-tier",
        "1",
        "--granted-tier",
        "1",
        "--standing-class",
        "trunk",
        "--anchor",
        f"{anchor_path}:1:1",
        role="verifier",
    )


def test_claim_grant_rejects_readout_statistics_end_to_end(tree: Sandbox) -> None:
    dispatch_id = _prepare_grant(tree)

    result = _grant(tree, dispatch_id, "readout/statistics.json")

    assert result.returncode != 0
    assert "readout/statistics.json" in result.stderr
    assert "[R-i6-3] mechanical path-class ban" in result.stderr


@pytest.mark.parametrize(
    "rel_path",
    [
        "readout/composed-tree.json",
        "readout/INTERPRETATION.md",
        "readout/observatory/OBS-1.json",
    ],
)
def test_readout_surfaces_are_rejected(sandbox: Sandbox, rel_path: str) -> None:
    if not (sandbox.root / rel_path).exists():
        sandbox.write_file(rel_path, "generated view\n")

    _assert_banned(Root(sandbox.root), rel_path)


@pytest.mark.parametrize(
    "rel_path",
    [
        "trees/L4/index.json",
        "trees/L4/index.live.json",
    ],
)
def test_tree_indexes_are_rejected(tree: Sandbox, rel_path: str) -> None:
    _assert_banned(Root(tree.root), rel_path)


def test_ledger_union_index_is_rejected(sandbox: Sandbox) -> None:
    _assert_banned(Root(sandbox.root), "ledger/union.index.json")


def test_traversal_normalizes_to_generated_view_and_reports_both_paths(
    tree: Sandbox,
) -> None:
    raw_path = "trees/L4/../../readout/statistics.json"

    _assert_banned(Root(tree.root), raw_path, "readout/statistics.json")


def test_real_outside_root_file_is_rejected_fail_closed(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-evidence.md"
    outside.write_text("real evidence outside the root\n", encoding="utf-8")
    raw_path = "../outside-evidence.md"

    with pytest.raises(HtError) as rejected:
        anchors.resolve(Root(sandbox.root), _anchor(raw_path))

    message = str(rejected.value)
    assert raw_path in message
    assert "[R-i6-3] fail-closed" in message


def test_symlink_laundering_into_readout_is_rejected(sandbox: Sandbox) -> None:
    rel_path = "system/notes/apparently-citable.md"
    link = sandbox.root / rel_path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(sandbox.root / "readout/statistics.json")

    _assert_banned(Root(sandbox.root), rel_path, "readout/statistics.json")


def test_nonexistent_banned_path_is_rejected_as_ban_not_missing(
    sandbox: Sandbox,
) -> None:
    rel_path = "readout/not-created-yet.json"

    with pytest.raises(HtError) as rejected:
        anchors.resolve(Root(sandbox.root), _anchor(rel_path))

    message = str(rejected.value)
    assert "[R-i6-3] mechanical path-class ban" in message
    assert "does not resolve to a file" not in message


def test_mixed_case_readout_is_banned_before_existence_check(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "scratch-root"
    root_path.mkdir()
    rel_path = "READOUT/statistics.json"
    assert not (root_path / rel_path).exists()

    with pytest.raises(HtError) as rejected:
        anchors.resolve(Root(root_path), _anchor(rel_path))

    message = str(rejected.value)
    assert rel_path in message
    assert "[R-i6-3] mechanical path-class ban" in message
    assert "does not resolve to a file" not in message


def test_legitimate_report_anchor_grants_claim(tree: Sandbox) -> None:
    dispatch_id = _prepare_grant(tree)
    report_path = f"trees/L4/nodes/1/reports/{dispatch_id}-report.md"

    result = _grant(tree, dispatch_id, report_path)

    assert result.returncode == 0, result.stderr


def test_observatory_source_file_remains_citable(sandbox: Sandbox) -> None:
    rel_path = "ledger/top/observatory/L-1.json"
    sandbox.write_file(rel_path, '{"observation": "canonical source"}\n')

    anchors.resolve(Root(sandbox.root), _anchor(rel_path))


def test_system_notes_anchor_resolves_at_unit_level(sandbox: Sandbox) -> None:
    rel_path = "system/notes/decision.md"
    sandbox.write_file(rel_path, "line one\nline two\n")

    anchors.resolve(Root(sandbox.root), _anchor(rel_path, 1, 2))


@pytest.mark.parametrize(
    "rel_path",
    [
        "readout",
        "readout/statistics.json",
        "readout/nested/anything.md",
        "index.json",
        "trees/L4/index.json",
        "index.live.json",
        "trees/L4/index.live.json",
        ".index.json",
        "ledger/union.index.json",
        "nested/custom.index.json",
    ],
)
def test_generated_view_predicate_denies_exact_path_classes(rel_path: str) -> None:
    assert classify.is_generated_view(rel_path)


@pytest.mark.parametrize(
    "rel_path",
    [
        "READOUT/statistics.json",
        "Readout/statistics.json",
        "trees/L4/INDEX.JSON",
        "ledger/UNION.INDEX.JSON",
    ],
)
def test_generated_view_predicate_denies_case_variants(rel_path: str) -> None:
    assert classify.is_generated_view(rel_path)


@pytest.mark.parametrize(
    "rel_path",
    [
        "readouts/statistics.json",
        "prefix/readout/statistics.json",
        "index.json.md",
        "trees/L4/index.json.md",
        "myindex.json",
        "trees/L4/myindex.json",
        "index.live.json.md",
        "ledger/union.index.json.md",
    ],
)
def test_generated_view_predicate_allows_near_misses(rel_path: str) -> None:
    assert not classify.is_generated_view(rel_path)
