"""Raw-Git regression tests for W2 committed-object discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unicodedata

import pytest

import composition_gate.discovery as discovery_module
from composition_gate.discovery import Discovery, TREE_STORE
from composition_gate.screen import CHECK_NAMES, render_screen, run_screen

from conftest import Sandbox


TREE_CHECKS = ("settlement-completeness", "watch-debt")
QUOTE_PATH_SETTINGS = ("unset", "true", "false")


def _record(record_id: str = "MR-1", *, surfaces: list[str] | None = None) -> dict:
    return {
        "id": record_id,
        "candidate_ref": "tree#candidate/node#1",
        "lane_verdict": "lane-pass",
        "scope": {
            "lane": "candidate",
            "seats": [],
            "surfaces": surfaces or [],
            "globs": [],
        },
        "screen": {"results": [], "log_ref": None},
        "gate_verdict": None,
        "watch_link": None,
        "created": "2026-07-13",
        "consumed_epoch": None,
    }


def _tree(component: str) -> dict:
    return {
        "component": component,
        "root_question": "Raw object discovery fixture",
        "epoch": 0,
        "epoch_history": [],
        "cursor": [],
        "decision_log": [],
        "global_learnings": [],
        "watch_queue": [],
    }


def _commit(sb: Sandbox, documents: dict[str, object], message: str) -> None:
    for path, value in documents.items():
        text = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
        sb.write_file(path, text)
    added = sb.git("add", "--", *documents)
    assert added.returncode == 0, added.stderr
    committed = sb.git("commit", "--no-verify", "-m", message)
    assert committed.returncode == 0, committed.stderr


def _set_quote_path(sb: Sandbox, setting: str) -> None:
    if setting == "unset":
        configured = sb.git("config", "--unset-all", "core.quotePath")
        assert configured.returncode in {0, 5}, configured.stderr
        return
    configured = sb.git("config", "core.quotePath", setting)
    assert configured.returncode == 0, configured.stderr


def _add_raw_index_path(
    sb: Sandbox, raw_path: bytes, oid: str, *, mode: bytes = b"100644"
) -> None:
    staged = subprocess.run(
        [
            b"git",
            b"-C",
            os.fsencode(sb.root),
            b"update-index",
            b"--add",
            b"--cacheinfo",
            mode + b"," + oid.encode("ascii") + b"," + raw_path,
        ],
        capture_output=True,
    )
    assert staged.returncode == 0, staged.stderr


def _remove_raw_index_path(sb: Sandbox, raw_path: bytes) -> None:
    removed = subprocess.run(
        [
            b"git",
            b"-C",
            os.fsencode(sb.root),
            b"update-index",
            b"--force-remove",
            b"--",
            raw_path,
        ],
        capture_output=True,
    )
    assert removed.returncode == 0, removed.stderr


def _entries(sb: Sandbox, ref: str) -> list[str]:
    result = sb.git("ls-tree", ref)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def _replace(
    lines: list[str], name: str, mode: str, object_type: str, oid: str
) -> list[str]:
    return [line for line in lines if not line.endswith("\t" + name)] + [
        f"{mode} {object_type} {oid}\t{name}"
    ]


def _mktree(sb: Sandbox, lines: list[str]) -> str:
    result = sb.git("mktree", input_text="\n".join(lines) + ("\n" if lines else ""))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit_tree(sb: Sandbox, root_tree: str, message: str) -> str:
    parent = sb.git("rev-parse", "HEAD")
    assert parent.returncode == 0, parent.stderr
    committed = sb.git(
        "commit-tree", root_tree, "-p", parent.stdout.strip(), input_text=message + "\n"
    )
    assert committed.returncode == 0, committed.stderr
    updated = sb.git("update-ref", "HEAD", committed.stdout.strip())
    assert updated.returncode == 0, updated.stderr
    return committed.stdout.strip()


def _raw_empty_component(sb: Sandbox, *, store: str, name: str) -> None:
    empty = _mktree(sb, [])
    if store == "trees":
        trees = _mktree(sb, _entries(sb, "HEAD:trees") + [f"040000 tree {empty}\t{name}"])
        root = _mktree(sb, _replace(_entries(sb, "HEAD"), "trees", "040000", "tree", trees))
    else:
        records = _mktree(
            sb,
            _entries(sb, "HEAD:tier1/merge-records")
            + [f"040000 tree {empty}\t{name}"],
        )
        tier1 = _mktree(
            sb,
            _replace(
                _entries(sb, "HEAD:tier1"),
                "merge-records",
                "040000",
                "tree",
                records,
            ),
        )
        root = _mktree(sb, _replace(_entries(sb, "HEAD"), "tier1", "040000", "tree", tier1))
    _commit_tree(sb, root, f"explicit empty {store} entry")


def _result(output: dict, name: str) -> dict:
    return next(row for row in output["results"] if row["check"] == name)


def test_explicit_empty_component_is_enumerated_and_fails_both_tree_consumers(
    sandbox: Sandbox,
):
    _commit(
        sandbox,
        {"tier1/merge-records/MR-1.json": _record(surfaces=["panel"])},
        "seed candidate",
    )
    _raw_empty_component(sandbox, store="trees", name="ghost")

    raw = sandbox.git("ls-tree", "HEAD", "--", "trees/ghost")
    assert "040000 tree" in raw.stdout
    output = run_screen(sandbox.root, "MR-1")

    for name in TREE_CHECKS:
        row = _result(output, name)
        assert row["result"] == "fail"
        assert "trees/ghost/ is missing exact tree.json" in row["detail"]
        assert row["inputs"]["discovery"]["stores"]["trees"]["status"] == "error"


def test_empty_subtree_below_merge_records_fails_every_record_consumer(
    sandbox: Sandbox,
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    _raw_empty_component(sandbox, store="tier1/merge-records", name="ghost")

    output = run_screen(sandbox.root, "MR-1")
    for name in CHECK_NAMES:
        row = _result(output, name)
        assert row["result"] == "fail"
        assert "unexpected committed merge-record path" in row["detail"]
        assert row["inputs"]["discovery"]["stores"]["tier1/merge-records"]["status"] == "error"


def test_valid_deep_empty_tree_is_present_in_inventory_without_becoming_document(
    sandbox: Sandbox,
):
    _commit(
        sandbox,
        {
            "tier1/merge-records/MR-1.json": _record(),
            "trees/observer/tree.json": _tree("observer"),
        },
        "seed valid tree",
    )
    empty = _mktree(sandbox, [])
    component = _mktree(
        sandbox,
        _entries(sandbox, "HEAD:trees/observer")
        + [f"040000 tree {empty}\tempty-deep"],
    )
    trees = _mktree(
        sandbox,
        _replace(
            _entries(sandbox, "HEAD:trees"),
            "observer",
            "040000",
            "tree",
            component,
        ),
    )
    root = _mktree(
        sandbox,
        _replace(_entries(sandbox, "HEAD"), "trees", "040000", "tree", trees),
    )
    _commit_tree(sandbox, root, "add deep empty tree")

    discovery = Discovery.capture(sandbox.root)
    inventory = discovery.inventory(TREE_STORE)
    assert inventory.by_path[("trees", "observer", "empty-deep")].object_type == "tree"
    assert [document.path for document in discovery.trees()] == [
        "trees/observer/tree.json"
    ]
    output = run_screen(sandbox.root, "MR-1")
    assert not any(_result(output, name)["detail"].startswith("screen-error:") for name in TREE_CHECKS)


@pytest.mark.parametrize("quote_setting", QUOTE_PATH_SETTINGS)
def test_invalid_utf8_deep_name_is_raw_and_config_invariant(
    sandbox: Sandbox, quote_setting: str
):
    _commit(
        sandbox,
        {
            "tier1/merge-records/MR-1.json": _record(),
            "trees/candidate/tree.json": _tree("candidate"),
        },
        "seed candidate and tree",
    )
    blob = sandbox.git("hash-object", "-w", "--stdin", input_text="")
    assert blob.returncode == 0, blob.stderr
    _add_raw_index_path(
        sandbox,
        b"trees/candidate/deep/bad-\xff.txt",
        blob.stdout.strip(),
    )
    committed = sandbox.git("commit", "--no-verify", "-m", "invalid UTF-8 deep name")
    assert committed.returncode == 0, committed.stderr
    _set_quote_path(sandbox, quote_setting)

    output = run_screen(sandbox.root, "MR-1")
    for name in TREE_CHECKS:
        row = _result(output, name)
        assert row["result"] == "fail"
        assert "invalid UTF-8 local name" in row["detail"]


@pytest.mark.parametrize("quote_setting", QUOTE_PATH_SETTINGS)
def test_nfc_nfd_siblings_and_nfd_only_deletion_are_config_invariant(
    sandbox: Sandbox, quote_setting: str
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    configured = sandbox.git("config", "core.precomposeunicode", "false")
    assert configured.returncode == 0, configured.stderr
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    tree_text = json.dumps(_tree(nfc), ensure_ascii=False, indent=2) + "\n"
    blob = sandbox.git("hash-object", "-w", "--stdin", input_text=tree_text)
    assert blob.returncode == 0, blob.stderr
    nfc_path = b"trees/" + nfc.encode("utf-8") + b"/tree.json"
    nfd_path = b"trees/" + nfd.encode("utf-8") + b"/tree.json"
    _add_raw_index_path(sandbox, nfc_path, blob.stdout.strip())
    _add_raw_index_path(sandbox, nfd_path, blob.stdout.strip())
    committed = sandbox.git("commit", "--no-verify", "-m", "NFC and NFD siblings")
    assert committed.returncode == 0, committed.stderr
    _set_quote_path(sandbox, quote_setting)

    with_twins = run_screen(sandbox.root, "MR-1")
    for name in TREE_CHECKS:
        row = _result(with_twins, name)
        assert row["result"] == "fail"
        assert "NFC+case-fold duplicate committed paths" in row["detail"]

    _remove_raw_index_path(sandbox, nfc_path)
    committed = sandbox.git("commit", "--no-verify", "-m", "delete NFC twin")
    assert committed.returncode == 0, committed.stderr
    nfd_only = run_screen(sandbox.root, "MR-1")
    for name in TREE_CHECKS:
        row = _result(nfd_only, name)
        assert row["result"] == "fail"
        assert "non-NFC committed path local name" in row["detail"]


def test_unicode_diagnostics_and_rendered_results_ignore_quote_path_config(
    sandbox: Sandbox,
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    configured = sandbox.git("config", "core.precomposeunicode", "false")
    assert configured.returncode == 0, configured.stderr
    nfc = unicodedata.normalize("NFC", "café")
    blob = sandbox.git("hash-object", "-w", "--stdin", input_text="")
    assert blob.returncode == 0, blob.stderr
    _add_raw_index_path(
        sandbox,
        b"trees/" + nfc.encode("utf-8") + b"/keep.txt",
        blob.stdout.strip(),
    )
    committed = sandbox.git("commit", "--no-verify", "-m", "Unicode diagnostic")
    assert committed.returncode == 0, committed.stderr

    rendered: list[str] = []
    for setting in QUOTE_PATH_SETTINGS:
        _set_quote_path(sandbox, setting)
        output = run_screen(sandbox.root, "MR-1")
        rendered.append(render_screen(output))
        for name in TREE_CHECKS:
            assert _result(output, name)["detail"] == (
                "screen-error: ScreenInputError: committed tree directory "
                "trees/café/ is missing exact tree.json"
            )
    assert len(set(rendered)) == 1


def test_list_tree_uses_default_raw_nul_records(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
):
    calls: list[list[str]] = []

    def runner(_root: Path, args: list[str]) -> bytes:
        calls.append(args)
        return b""

    monkeypatch.setattr(discovery_module, "_run_git_binary", runner)
    tree_oid = "c" * 40
    snapshot = discovery_module.GitSnapshot(
        sandbox.root, "a" * 40, "b" * 40, "2026-07-13T00:00:00+03:00"
    )
    assert Discovery(snapshot)._list_tree(tree_oid) == ()
    assert calls == [["ls-tree", "-z", "--full-tree", tree_oid]]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"100644 blob " + b"a" * 40 + b"\tname", "incomplete NUL record"),
        (
            b"100644 blob " + b"a" * 40 + b" name\0",
            "expected mode SP type SP oid TAB raw-name",
        ),
        (
            b"100644  blob " + b"a" * 40 + b"\tname\0",
            "expected mode SP type SP oid TAB raw-name",
        ),
        (
            b"100644 bl\xffb " + b"a" * 40 + b"\tname\0",
            "invalid ASCII metadata",
        ),
        (b"100644 blob " + b"g" * 40 + b"\tname\0", "invalid object id"),
        (b"100644 blob " + b"a" * 40 + b"\t\0", "unsafe local name"),
    ],
)
def test_list_tree_rejects_malformed_raw_record_framing_and_metadata(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    message: str,
):
    monkeypatch.setattr(discovery_module, "_run_git_binary", lambda _root, _args: raw)
    snapshot = discovery_module.GitSnapshot(
        sandbox.root, "a" * 40, "b" * 40, "2026-07-13T00:00:00+03:00"
    )
    with pytest.raises(discovery_module.ScreenInputError, match=message):
        Discovery(snapshot)._list_tree("c" * 40)


@pytest.mark.parametrize("mode", ["100755", "120000"])
@pytest.mark.parametrize("quote_setting", QUOTE_PATH_SETTINGS)
def test_noncanonical_json_blob_modes_fail_closed(
    sandbox: Sandbox, mode: str, quote_setting: str
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    oid = sandbox.git("rev-parse", "HEAD:tier1/merge-records/MR-1.json")
    assert oid.returncode == 0, oid.stderr
    staged = sandbox.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{oid.stdout.strip()},tier1/merge-records/MR-1.json",
    )
    assert staged.returncode == 0, staged.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", f"candidate mode {mode}")
    assert committed.returncode == 0, committed.stderr
    _set_quote_path(sandbox, quote_setting)

    output = run_screen(sandbox.root, "MR-1")
    assert all(row["result"] == "fail" for row in output["results"])
    assert all(f"{mode} blob" in row["detail"] for row in output["results"])


@pytest.mark.parametrize("quote_setting", QUOTE_PATH_SETTINGS)
def test_gitlink_under_selected_store_fails_closed(
    sandbox: Sandbox, quote_setting: str
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    head = sandbox.git("rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    staged = sandbox.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{head.stdout.strip()},trees/submodule",
    )
    assert staged.returncode == 0, staged.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "gitlink under trees")
    assert committed.returncode == 0, committed.stderr
    _set_quote_path(sandbox, quote_setting)

    output = run_screen(sandbox.root, "MR-1")
    for name in TREE_CHECKS:
        row = _result(output, name)
        assert row["result"] == "fail"
        assert "160000 commit" in row["detail"]
    assert _result(output, "scope-overlap")["result"] == "pass"


def test_missing_and_wrong_type_tree_anchor_have_exact_nullable_citations(
    sandbox: Sandbox,
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    removed = sandbox.git("rm", "trees/.gitkeep")
    assert removed.returncode == 0, removed.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "remove tree anchor")
    assert committed.returncode == 0, committed.stderr

    missing = _result(run_screen(sandbox.root, "MR-1"), "watch-debt")
    citation = missing["inputs"]["discovery"]["stores"]["trees"]
    assert citation["status"] == "error"
    assert citation["tree_oid"] is None
    assert citation["observed"] is None
    assert "missing exact committed anchor trees" in citation["error"]["message"]

    empty_blob = sandbox.git("hash-object", "-w", "--stdin", input_text="")
    assert empty_blob.returncode == 0, empty_blob.stderr
    root = _mktree(
        sandbox,
        _replace(
            _entries(sandbox, "HEAD"),
            "trees",
            "100644",
            "blob",
            empty_blob.stdout.strip(),
        ),
    )
    _commit_tree(sandbox, root, "wrong type tree anchor")
    wrong = _result(run_screen(sandbox.root, "MR-1"), "watch-debt")
    citation = wrong["inputs"]["discovery"]["stores"]["trees"]
    assert citation["status"] == "error"
    assert citation["tree_oid"] is None
    assert citation["observed"] == {
        "mode": "100644",
        "type": "blob",
        "oid": empty_blob.stdout.strip(),
    }


def test_normalized_anchor_alias_is_structural_error(sandbox: Sandbox):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    trees = sandbox.git("rev-parse", "HEAD:trees")
    assert trees.returncode == 0, trees.stderr
    lines = [line for line in _entries(sandbox, "HEAD") if not line.endswith("\ttrees")]
    root = _mktree(sandbox, lines + [f"040000 tree {trees.stdout.strip()}\tTrees"])
    _commit_tree(sandbox, root, "alias trees anchor")

    row = _result(run_screen(sandbox.root, "MR-1"), "watch-debt")
    assert row["result"] == "fail"
    assert "normalized alias for required committed anchor trees" in row["detail"]


@pytest.mark.parametrize("raw_name", [b"bad-\xff.json", b"line\nbreak.json", b"tab\tbreak.json"])
@pytest.mark.parametrize("quote_setting", QUOTE_PATH_SETTINGS)
def test_raw_merge_record_names_are_never_silently_omitted(
    sandbox: Sandbox, raw_name: bytes, quote_setting: str
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "seed candidate")
    blob = sandbox.git("hash-object", "-w", "--stdin", input_text="{}")
    assert blob.returncode == 0, blob.stderr
    path = b"tier1/merge-records/" + raw_name
    staged = subprocess.run(
        [
            b"git",
            b"-C",
            os.fsencode(sandbox.root),
            b"update-index",
            b"--add",
            b"--cacheinfo",
            b"100644," + blob.stdout.strip().encode("ascii") + b"," + path,
        ],
        capture_output=True,
    )
    assert staged.returncode == 0, staged.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "raw pathname")
    assert committed.returncode == 0, committed.stderr
    _set_quote_path(sandbox, quote_setting)

    first = render_screen(run_screen(sandbox.root, "MR-1"))
    second = render_screen(run_screen(sandbox.root, "MR-1"))
    assert first == second
    output = json.loads(first)
    assert all(row["result"] == "fail" for row in output["results"])
    assert all(row["detail"].startswith("screen-error:") for row in output["results"])


def test_snapshot_remains_pinned_when_head_moves_during_enumeration(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
):
    _commit(sandbox, {"tier1/merge-records/MR-1.json": _record()}, "first candidate")
    first = sandbox.git("rev-parse", "HEAD").stdout.strip()
    first_output = run_screen(sandbox.root, "MR-1")
    changed = _record(surfaces=["new-surface"])
    _commit(sandbox, {"tier1/merge-records/MR-1.json": changed}, "second candidate")
    second = sandbox.git("rev-parse", "HEAD").stdout.strip()
    assert sandbox.git("update-ref", "HEAD", first).returncode == 0

    original = discovery_module._run_git_binary
    moved = False

    def moving_runner(root: Path, args: list[str]) -> bytes:
        nonlocal moved
        output = original(root, args)
        if args and args[0] == "ls-tree" and not moved:
            moved = True
            assert sandbox.git("update-ref", "HEAD", second).returncode == 0
        return output

    monkeypatch.setattr(discovery_module, "_run_git_binary", moving_runner)
    pinned = run_screen(sandbox.root, "MR-1")

    assert moved is True
    assert pinned["head_commit"] == first
    assert pinned["head_tree"] == first_output["head_tree"]
    assert pinned["computed"] == first_output["computed"]
    assert render_screen(pinned) == render_screen(first_output)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == second


def test_timeout_terminates_waits_discards_partial_output_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = 0

    class TimedOutProcess:
        returncode = None
        terminated = False
        waited = False

        def communicate(self, *, timeout: float):
            nonlocal calls
            calls += 1
            assert timeout == 30.0
            raise subprocess.TimeoutExpired(["git"], timeout, output=b"partial")

        def terminate(self) -> None:
            self.terminated = True

        def wait(self) -> int:
            self.waited = True
            self.returncode = -15
            return -15

    process = TimedOutProcess()
    monkeypatch.setattr(discovery_module.subprocess, "Popen", lambda *a, **k: process)

    with pytest.raises(discovery_module.ScreenInputError) as error:
        discovery_module._run_git_binary(tmp_path, ["rev-parse", "HEAD"])

    assert str(error.value) == "git command timed out after 30.0s"
    assert calls == 1
    assert process.terminated is True
    assert process.waited is True
    assert "partial" not in str(error.value)


def test_snapshot_capture_failure_has_exact_error_and_not_attempted_store_shape(
    tmp_path: Path,
):
    output = run_screen(tmp_path / "not-a-repository", "MR-1")
    assert output["head_commit"] is None
    assert output["head_tree"] is None
    for row in output["results"]:
        discovery = row["inputs"]["discovery"]
        assert discovery["snapshot"]["status"] == "error"
        assert discovery["snapshot"]["head_commit"] is None
        assert discovery["snapshot"]["head_tree"] is None
        assert all(store["status"] == "not-attempted" for store in discovery["stores"].values())
