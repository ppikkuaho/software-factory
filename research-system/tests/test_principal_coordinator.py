from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import Sandbox
from ht import composed_tree, jsonio
from ht.commands import pc as pc_command
from ht.paths import Root as ResearchRoot


ROOT = Path(__file__).resolve().parents[1]
_SOURCE_PYTHONPATH = os.pathsep.join(
    (
        str(ROOT / "system/instruments/composition-gate"),
        str(ROOT / "system"),
    )
)


def _run_pc_spawn(sandbox: Sandbox, *args: str):
    """Run only the new PC packet surface against co-versioned sources."""

    return sandbox.run(
        "pc",
        "spawn",
        *args,
        env_extra={"PYTHONPATH": _SOURCE_PYTHONPATH},
    )


def _gate(verdict: str, number: int) -> dict:
    return {
        "verdict": verdict,
        "date": "2026-07-14",
        "review_ref": f"GR-{number}",
        "review_sha256": f"{number:064x}",
        "note": f"Synthetic {verdict} linkage for PC.",
    }


def _merge_record(
    record_id: str,
    *,
    verdict: str | None = None,
    consumed_epoch: int | None = None,
) -> dict:
    number = int(record_id.removeprefix("MR-"))
    return {
        "id": record_id,
        "candidate_ref": f"tree#L4/node#{number}",
        "lane_verdict": "lane-pass",
        "scope": {
            "lane": "L4",
            "seats": [f"seat-{number}"],
            "surfaces": [f"surface-{number}"],
            "globs": [f"trees/L4/nodes/{number}/**"],
        },
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
        "gate_verdict": None if verdict is None else _gate(verdict, number),
        "watch_link": None,
        "created": "2026-07-14",
        "consumed_epoch": consumed_epoch,
    }


def _commit_merge_records(sandbox: Sandbox, records: list[dict]) -> None:
    relative_paths = []
    for record in records:
        relative = f"tier1/merge-records/{record['id']}.json"
        sandbox.write_file(relative, jsonio.dumps(record))
        relative_paths.append(relative)
    added = sandbox.git("add", "--", *relative_paths)
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "synthetic PC records")
    assert committed.returncode == 0, committed.stderr


def _packet(document: str) -> dict:
    marker = "## Surface pointers and bounded state\n\n```json\n"
    payload = document.split(marker, 1)[1].split("\n```", 1)[0]
    return json.loads(payload)


def _physical_state(sandbox: Sandbox) -> tuple:
    def git_output(*args: str) -> str:
        result = sandbox.git(*args)
        assert result.returncode == 0, result.stderr
        return result.stdout

    var_rows = []
    var_root = sandbox.root / "var"
    if var_root.exists():
        for directory, _directories, files in os.walk(var_root):
            for filename in files:
                path = Path(directory) / filename
                var_rows.append(
                    (path.relative_to(sandbox.root).as_posix(), path.read_bytes())
                )
    return (
        git_output("rev-parse", "HEAD"),
        git_output("diff", "--raw"),
        git_output("diff", "--cached", "--raw"),
        git_output("status", "--porcelain=v1", "--untracked-files=all"),
        tuple(sorted(var_rows)),
    )


def _expected_pending_row(record: dict, *, pending: bool, landed: bool) -> dict:
    return {
        "id": record["id"],
        "candidate_ref": record["candidate_ref"],
        "scope": record["scope"],
        "gate_verdict": record["gate_verdict"],
        "memberships": {"pending": pending, "landed": landed},
    }


def _mint_issue(sb: Sandbox) -> None:
    result = sb.run(
        "issue", "mint",
        "--title", "Cross-lane pressure",
        "--question", "Where does the pressure originate?",
        "--done-definition", "One attribution is supported or all are escalated.",
        "--provenance", "user-seed#commissioning",
        "--lanes", "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr


def _activate_issue(sb: Sandbox) -> None:
    _mint_issue(sb)
    assert sb.run("issue", "ratify", "--issue", "I-1", role="user").returncode == 0
    assert sb.run("issue", "activate", "--issue", "I-1", role="user").returncode == 0


def test_issue_queue_is_current_state_only_and_pc_owned(sandbox: Sandbox) -> None:
    schema = json.loads((ROOT / "system/schemas/issue_queue.schema.json").read_text())
    assert schema["$comment"] == (
        "CURRENT-STATE only; history lives in the decision log; material re-ranks "
        "are logged decisions per PC §5."
    )
    _mint_issue(sandbox)

    rejected = sandbox.run("pcq", "set", "--entry", "I-1:1:urgent", role="director")
    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert "owner: pc" in rejected.stderr

    for role in ("verifier", "unit", "harness", "cgate"):
        wrong = sandbox.run("pcq", "set", "--entry", "I-1:1:urgent", role=role)
        assert wrong.returncode != 0
        assert wrong.stdout == ""
        assert "owner: pc" in wrong.stderr

    set_result = sandbox.run(
        "pcq", "set", "--entry", "I-1:1:urgent", "--date", "2026-07-13", role="pc"
    )
    assert set_result.returncode == 0, set_result.stderr
    assert "FULL-DOCUMENT REPLACE" in set_result.stderr
    assert "CURRENT-STATE only" in set_result.stderr

    override = sandbox.run(
        "pcq", "set", "--entry", "I-1:1:user-override",
        "--date", "2026-07-13", role="user"
    )
    assert override.returncode == 0, override.stderr

    shown = sandbox.run("pcq", "show")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == {
        "entries": [
            {
                "issue_ref": "I-1",
                "rank": 1,
                "triage_note": "user-override",
                "date": "2026-07-13",
            }
        ]
    }


def test_pcd_append_kinds_refs_and_append_only_hook(sandbox: Sandbox) -> None:
    assert sandbox.run(
        "interrupt", "create", "--raised-by", "L4", "--issue-ref", "I-1",
        "--sub-goal-ref", "node#7", "--rationale", "Needs reframing.",
        role="director",
    ).returncode == 0
    added = sandbox.run(
        "pcd", "append",
        "--kind", "interrupt-receipt",
        "--ref", "INT-1",
        "--text", "Reframed and returned to the L4 director.",
        "--date", "2026-07-13",
        role="pc",
    )
    assert added.returncode == 0, added.stderr
    doc = sandbox.load("tier1/decision-log/PCD-1.json")
    assert doc["kind"] == "interrupt-receipt"
    assert doc["ref"] == "INT-1"
    assert doc["context_refs"] == ["INT-1"]

    doc["decision"] = "rewritten"
    sandbox.write_file("tier1/decision-log/PCD-1.json", json.dumps(doc))
    assert sandbox.git("add", "tier1/decision-log/PCD-1.json").returncode == 0
    commit = sandbox.git(
        "commit", "-m", "rewrite decision",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "pc"},
    )
    assert commit.returncode != 0
    assert "append-only" in commit.stderr


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("activation-request", "pc"),
        ("cgate-escalation", "cgate"),
        ("tier3-ratification", "verifier"),
        ("improvement-note", "harness"),
    ],
)
def test_rq_kind_creator_pairs_and_wrong_roles(
    sandbox: Sandbox, kind: str, role: str
) -> None:
    wrong = next(candidate for candidate in ("pc", "cgate", "verifier", "harness") if candidate != role)
    rejected = sandbox.run(
        "rq", "append", "--kind", kind,
        "--payload-ref", "payload#1", "--text", "Review it.", role=wrong,
    )
    assert rejected.returncode != 0
    assert "kind-selected creator" in rejected.stderr

    accepted = sandbox.run(
        "rq", "append", "--kind", kind,
        "--payload-ref", "payload#1", "--text", "Review it.",
        "--date", "2026-07-13", role=role,
    )
    assert accepted.returncode == 0, accepted.stderr
    doc = sandbox.load("tier1/ratification-queue/RQ-1.json")
    assert doc["queued_by"] == role
    assert doc["disposition"] is None


def test_rq_annotation_disposition_and_pending_first_list(sandbox: Sandbox) -> None:
    for number in (1, 2):
        result = sandbox.run(
            "rq", "append", "--kind", "activation-request",
            "--payload-ref", f"issue#I-{number}", "--text", "Activate?", role="pc",
        )
        assert result.returncode == 0, result.stderr

    wrong_annotation = sandbox.run(
        "rq", "annotate", "--item", "RQ-1", "--note", "context", role="user"
    )
    assert wrong_annotation.returncode != 0
    assert "owner: pc" in wrong_annotation.stderr
    assert sandbox.run(
        "rq", "annotate", "--item", "RQ-1", "--note", "Needs sequencing.", role="pc"
    ).returncode == 0

    non_user = sandbox.run(
        "rq", "dispose", "--item", "RQ-1", "--status", "deferred",
        "--by", "principal-coordinator", role="pc",
    )
    assert non_user.returncode != 0
    assert "owner: user" in non_user.stderr
    disposed = sandbox.run(
        "rq", "dispose", "--item", "RQ-1", "--status", "deferred",
        "--by", "user", "--note", "Wait for lane report.", role="user",
    )
    assert disposed.returncode == 0, disposed.stderr
    frozen = sandbox.run(
        "rq", "dispose", "--item", "RQ-1", "--status", "accepted",
        "--by", "user", role="user",
    )
    assert frozen.returncode != 0
    assert "frozen" in frozen.stderr

    listed = sandbox.run("rq", "list")
    assert listed.returncode == 0, listed.stderr
    assert [row["id"] for row in json.loads(listed.stdout)] == ["RQ-2", "RQ-1"]


def test_interrupt_blocks_issue_close_until_pcd_receipt(sandbox: Sandbox) -> None:
    _activate_issue(sandbox)
    assert sandbox.run(
        "tree", "init", "L4", "--root-question", "Synthetic interrupt root",
        role="director",
    ).returncode == 0
    assert sandbox.run(
        "node", "mint", "--tree", "L4", "--root",
        "--premise", "Synthetic interrupt sub-goal",
        "--from-issue", "I-1", "--rationale", "interrupt fixture",
        role="director",
    ).returncode == 0
    assert sandbox.run(
        "issue", "subgoal-add", "--issue", "I-1", "--ref", "node#1", role="pc"
    ).returncode == 0

    for role in ("pc", "user", "verifier", "unit", "harness", "cgate"):
        wrong = sandbox.run(
            "interrupt", "create", "--raised-by", "L4", "--issue-ref", "I-1",
            "--sub-goal-ref", "node#1", "--rationale", "The premise is underspecified.",
            role=role,
        )
        assert wrong.returncode != 0
        assert wrong.stdout == ""
        assert "creator: director" in wrong.stderr
    raised = sandbox.run(
        "interrupt", "raise", "--lane", "L4", "--issue-ref", "I-1",
        "--sub-goal-ref", "node#1", "--rationale", "The premise is underspecified.",
        role="director",
    )
    assert raised.returncode == 0, raised.stderr
    assert sandbox.load("tier1/interrupts/INT-1.json")["issue_ref"] == "I-1"

    blocked = sandbox.run(
        "issue", "close", "--issue", "I-1", "--text", "Settled.", role="pc"
    )
    assert blocked.returncode != 0
    assert blocked.stdout == ""
    assert "undispositioned interrupts: INT-1" in blocked.stderr
    assert sandbox.load("tier1/issues/I-1.json")["closure"] is None

    receipt = sandbox.run(
        "pcd", "append", "--kind", "interrupt-receipt", "--ref", "INT-1",
        "--text", "Reframed the sub-goal and returned it.", role="pc",
    )
    assert receipt.returncode == 0, receipt.stderr
    closed = sandbox.run(
        "issue", "close", "--issue", "I-1", "--text", "Settled.", role="pc"
    )
    assert closed.returncode == 0, closed.stderr


def test_interrupt_receipt_requires_typed_interrupt_ref(sandbox: Sandbox) -> None:
    missing = sandbox.run(
        "pcd", "append", "--kind", "interrupt-receipt",
        "--text", "Acknowledged without an identity.", role="pc",
    )
    assert missing.returncode != 0
    assert missing.stdout == ""
    assert "requires --ref INT-<n>" in missing.stderr
    unknown = sandbox.run(
        "pcd", "append", "--kind", "interrupt-receipt", "--ref", "INT-99",
        "--text", "Receipt for a missing interrupt.", role="pc",
    )
    assert unknown.returncode != 0
    assert "unknown interrupt 'INT-99'" in unknown.stderr


def test_interrupt_is_frozen_and_tier1_delete_ban_applies(sandbox: Sandbox) -> None:
    raised = sandbox.run(
        "interrupt", "create", "--raised-by", "L4", "--issue-ref", "I-1",
        "--sub-goal-ref", "node#1", "--rationale", "Cannot complete honestly.",
        role="director",
    )
    assert raised.returncode == 0, raised.stderr
    rel = "tier1/interrupts/INT-1.json"
    doc = sandbox.load(rel)
    doc["rationale"] = "rewritten"
    sandbox.write_file(rel, json.dumps(doc))
    assert sandbox.git("add", rel).returncode == 0
    modified = sandbox.git(
        "commit", "-m", "rewrite interrupt",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )
    assert modified.returncode != 0
    assert "frozen after creation" in modified.stderr
    assert sandbox.git("reset", "--hard", "HEAD").returncode == 0

    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0
    deleted = sandbox.git(
        "commit", "-m", "delete interrupt",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )
    assert deleted.returncode != 0
    assert "may not vanish" in deleted.stderr
    assert sandbox.git("reset", "--hard", "HEAD").returncode == 0

    destination = "notes/INT-1.json"
    (sandbox.root / "notes").mkdir()
    assert sandbox.git("mv", rel, destination).returncode == 0
    renamed = sandbox.git(
        "commit", "-m", "rename interrupt",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )
    assert renamed.returncode != 0
    assert "may not vanish" in renamed.stderr


def test_composed_tree_is_deterministic_multi_tree_union_with_issue_edges(
    sandbox: Sandbox,
) -> None:
    _mint_issue(sandbox)
    for component in ("L4", "L5"):
        assert sandbox.run(
            "tree", "init", component, "--root-question", f"Question for {component}?",
            role="director",
        ).returncode == 0
        assert sandbox.run(
            "node", "mint", "--tree", component, "--root",
            "--premise", f"Premise in {component}", "--from-issue", "I-1",
            "--rationale", "commissioning", role="director",
        ).returncode == 0
    assert sandbox.run(
        "dispatch", "create", "--tree", "L4", "--node", "1",
        "--question", "Test it", "--done-definition", "Return a result",
        "--issue-ref", "I-1", role="director",
    ).returncode == 0

    projection_path = sandbox.root / "readout/composed-tree.json"
    first = projection_path.read_bytes()
    first_build = jsonio.dumps(composed_tree.build(ResearchRoot(sandbox.root)))
    second_build = jsonio.dumps(composed_tree.build(ResearchRoot(sandbox.root)))
    assert second_build.encode("utf-8") == first_build.encode("utf-8") == first
    validate = sandbox.run("validate", "--all")
    assert validate.returncode == 0, validate.stderr
    second = projection_path.read_bytes()
    assert second == first

    projection = json.loads(first)
    assert projection["generated"] is True
    assert projection["citable"] is False
    assert [tree["component"] for tree in projection["trees"]] == ["L4", "L5"]
    canonical = sandbox.load("trees/L4/nodes/1/node.json")
    copied = projection["trees"][0]["nodes"][0]["node"]
    assert copied == canonical
    assert copied["standing"] == canonical["standing"]
    assert "derived_standing" not in json.dumps(projection)
    assert {edge["source"] for edge in projection["issue_edges"]} == {
        "minted_from", "issue_ref"
    }


def test_pc_spawn_dry_run_composes_resolving_surfaces_without_launch(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    output = tmp_path / "pc-launch.md"
    result = _run_pc_spawn(
        sandbox, "--dry-run", "--out", str(output), "--decision-tail", "3"
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == result.stdout
    assert "You are the **principal coordinator**" in result.stdout
    assert '"seat": "ht-pc"' in result.stdout
    assert "ledger/union.index.json" in result.stdout
    assert "readout/composed-tree.json" in result.stdout
    assert "readout/INTERPRETATION.md" in result.stdout
    assert "tier1/issue-queue.json" in result.stdout
    assert "bus enable --name ht-pc" in result.stdout


def test_pc_spawn_dry_run_never_invokes_launch_subprocess(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    invoked = False
    real_run = subprocess.run

    def forbid_launch_only(args, *run_args, **run_kwargs):
        nonlocal invoked
        if args and args[0] == "tmux":
            invoked = True
            raise AssertionError("dry-run invoked launch subprocess")
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(pc_command.subprocess, "run", forbid_launch_only)
    assert pc_command.spawn(
        ResearchRoot(sandbox.root),
        dry_run=True,
        launch=False,
        out=None,
        decision_tail=10,
    ) == 0
    assert "Principal coordinator launch document" in capsys.readouterr().out
    assert invoked is False


def test_pc_packet_carries_item6_style_and_rider_pins() -> None:
    text = (ROOT / "system/roles/principal-coordinator.v1.md").read_text()
    _, _, body = text.split("---\n", 2)
    assert "You are the **principal coordinator**" in body
    assert "No raw material, ever" in body
    assert "CURRENT-STATE only; history lives in the decision" in body
    assert "material re-ranks are logged decisions" in body
    assert "Read, annotate, notify, and recommend on the ratification queue — never dispose" in body
    assert "only the user may ratify or activate an issue" in body
    assert "Never\ninfer autonomy from smooth operation or observed reliability" in body
    assert "generated and uncitable" in body
    assert "No standing is\nre-derived" in body
    assert "--kind interrupt-receipt --ref INT-<n>" in body


def test_pc_packet_pending_merges_is_exact_committed_role_free_and_read_only(
    sandbox: Sandbox,
) -> None:
    records = {
        record["id"]: record
        for record in (
            _merge_record("MR-10", verdict="consolidate-first"),
            _merge_record("MR-2"),
            _merge_record("MR-20", verdict="land", consumed_epoch=9),
            _merge_record("MR-3", verdict="hold"),
            _merge_record("MR-7", verdict="land"),
        )
    }
    _commit_merge_records(sandbox, list(records.values()))

    # Neither a changed committed path nor an MR-shaped untracked file may
    # leak into the packet's captured committed-state view.
    sandbox.write_file(
        "tier1/merge-records/MR-2.json",
        jsonio.dumps(_merge_record("MR-2", verdict="escalate-to-user")),
    )
    sandbox.write_file(
        "tier1/merge-records/MR-99.json",
        jsonio.dumps(_merge_record("MR-99", verdict="land")),
    )
    before = _physical_state(sandbox)

    result = _run_pc_spawn(sandbox, "--dry-run", "--decision-tail", "0")

    assert result.returncode == 0, result.stderr
    assert _packet(result.stdout)["pending_merges"] == {
        "records": [
            _expected_pending_row(records["MR-2"], pending=True, landed=False),
            _expected_pending_row(records["MR-3"], pending=False, landed=True),
            _expected_pending_row(records["MR-7"], pending=True, landed=True),
            _expected_pending_row(records["MR-10"], pending=False, landed=True),
        ],
        "counts": {
            "total_unconsumed": 4,
            "pending_view": 2,
            "landed_view": 3,
            "by_partition": {
                "awaiting-verdict": 1,
                "land-ready": 1,
                "verdict-issued/unconsumed": 2,
            },
            "by_verdict": {
                "consolidate-first": 1,
                "hold": 1,
                "land": 1,
            },
        },
    }
    assert records["MR-10"]["gate_verdict"] == _packet(result.stdout)[
        "pending_merges"
    ]["records"][3]["gate_verdict"]
    assert "MR-20" not in json.dumps(_packet(result.stdout)["pending_merges"])
    assert "MR-99" not in json.dumps(_packet(result.stdout)["pending_merges"])
    assert _physical_state(sandbox) == before


def test_pc_compose_loads_one_snapshot_and_one_unconsumed_view(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ht import mrec_views

    calls = {"load": 0, "unconsumed": 0}
    record = _merge_record("MR-4", verdict="land")

    class Entry:
        pending = True
        landed = True
        partition = "land-ready"

        def as_dict(self) -> dict:
            return record

    class Snapshot:
        def unconsumed(self) -> tuple[Entry, ...]:
            calls["unconsumed"] += 1
            return (Entry(),)

    def load_once(root: Path) -> Snapshot:
        calls["load"] += 1
        assert root == sandbox.root
        return Snapshot()

    monkeypatch.setattr(mrec_views, "load_merge_record_snapshot", load_once)
    document = pc_command.compose(ResearchRoot(sandbox.root), decision_tail=0)

    assert calls == {"load": 1, "unconsumed": 1}
    assert _packet(document)["pending_merges"] == {
        "records": [
            _expected_pending_row(record, pending=True, landed=True),
        ],
        "counts": {
            "total_unconsumed": 1,
            "pending_view": 1,
            "landed_view": 1,
            "by_partition": {
                "awaiting-verdict": 0,
                "land-ready": 1,
                "verdict-issued/unconsumed": 0,
            },
            "by_verdict": {"land": 1},
        },
    }


def test_pc_selector_failure_precedes_stdout_and_output_file(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    relative = "tier1/merge-records/MR-1.json"
    sandbox.write_file(relative, "{")
    assert sandbox.git("add", "--", relative).returncode == 0
    committed = sandbox.git("commit", "--no-verify", "-m", "malformed PC record")
    assert committed.returncode == 0, committed.stderr
    output = tmp_path / "must-not-exist.md"
    before = _physical_state(sandbox)

    result = _run_pc_spawn(
        sandbox,
        "--dry-run",
        "--out",
        str(output),
        "--decision-tail",
        "0",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "merge-record snapshot discovery failed" in result.stderr
    assert not output.exists()
    assert _physical_state(sandbox) == before


def test_pc_module_import_does_not_eagerly_import_merge_record_selector() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "system")
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import ht.commands.pc; "
                "assert 'ht.mrec_views' not in sys.modules"
            ),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert imported.returncode == 0, imported.stderr
