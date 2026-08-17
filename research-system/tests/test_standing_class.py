"""Coherence amendments §10 evidence classing and trunk re-validation gates."""

from __future__ import annotations

import json

import pytest

from conftest import Sandbox, finalize_gate_decision, transcribe_engine_screen
from ht import schemas
from ht.commands import _common, node as node_cmd
from ht.commands._common import Ctx
from ht.errors import HtError
from ht.paths import Root
from ht.references import ADJUDICATION_HEADER_PREFIX


def _claim_doc(**overrides) -> dict:
    document = {
        "id": "c-1-1",
        "node": "1",
        "source_dispatch": "d-1-1",
        "text": "The claim holds under the measured conditions.",
        "proposed_tier": 2,
        "granted_tier": 2,
        "standing_class": "trunk",
        "revalidation": None,
        "anchors": [{"path": "report.md", "start_line": 1, "end_line": 1}],
        "epoch": 0,
        "instruments": [],
        "status": "granted",
    }
    document.update(overrides)
    return document


def _measurement_doc(**overrides) -> dict:
    document = {
        "metric": "latency_ms",
        "value": 12.5,
        "epoch": 0,
        "instrument": "suite-S@v1",
        "noise_flag": False,
        "standing_class": "trunk",
        "anchors": [{"path": "report.md", "start_line": 1, "end_line": 1}],
    }
    document.update(overrides)
    return document


def _ready_dispatch(sb: Sandbox) -> tuple[str, str]:
    minted = sb.run(
        "node",
        "mint",
        "--tree",
        "L4",
        "--root",
        "--premise",
        "Classify this evidence",
        "--rationale",
        "standing-class test",
        role="director",
    )
    assert minted.returncode == 0, minted.stderr
    created = sb.run(
        "dispatch",
        "create",
        "--node",
        "1",
        "--question",
        "What does the evidence support?",
        "--done-definition",
        "A classed claim is adjudicated",
        role="director",
    )
    assert created.returncode == 0, created.stderr
    source = sb.write_file("standing-report.md", "one\ntwo\nthree\n")
    submitted = sb.run(
        "report",
        "submit",
        "--dispatch",
        "d-1-1",
        "--src",
        str(source),
        role="unit",
    )
    assert submitted.returncode == 0, submitted.stderr
    return "d-1-1", "trees/L4/nodes/1/reports/d-1-1-report.md:1:2"


def _grant(sb: Sandbox, standing_class: str = "trunk"):
    dispatch_id, anchor = _ready_dispatch(sb)
    return sb.run(
        "claim",
        "grant",
        "--dispatch",
        dispatch_id,
        "--text",
        "The evidence supports the claim.",
        "--proposed-tier",
        "2",
        "--granted-tier",
        "2",
        "--standing-class",
        standing_class,
        "--anchor",
        anchor,
        role="verifier",
    )


def _grant_after_epoch_advance(sb: Sandbox) -> str:
    first_grant = _grant(sb)
    assert first_grant.returncode == 0, first_grant.stderr
    record_id = _create_land_record(sb)
    merged = _merge(sb, record_id)
    assert merged.returncode == 0, merged.stderr

    minted = sb.run(
        "node",
        "mint",
        "--tree",
        "L4",
        "--root",
        "--premise",
        "Revalidate evidence after trunk advances",
        "--rationale",
        "epoch-ordering test",
        role="director",
    )
    assert minted.returncode == 0, minted.stderr
    created = sb.run(
        "dispatch",
        "create",
        "--node",
        "2",
        "--question",
        "Does the post-merge evidence hold?",
        "--done-definition",
        "A claim is granted at epoch one",
        role="director",
    )
    assert created.returncode == 0, created.stderr
    source = sb.write_file("epoch-one-report.md", "one\ntwo\nthree\n")
    submitted = sb.run(
        "report",
        "submit",
        "--dispatch",
        "d-2-1",
        "--src",
        str(source),
        role="unit",
    )
    assert submitted.returncode == 0, submitted.stderr
    granted = sb.run(
        "claim",
        "grant",
        "--dispatch",
        "d-2-1",
        "--text",
        "The post-merge evidence supports the claim.",
        "--proposed-tier",
        "2",
        "--granted-tier",
        "2",
        "--standing-class",
        "sandbox",
        "--anchor",
        "trees/L4/nodes/2/reports/d-2-1-report.md:1:2",
        role="verifier",
    )
    assert granted.returncode == 0, granted.stderr
    assert sb.load("trees/L4/nodes/2/node.json")["claims"][0]["epoch"] == 1
    return "c-2-1"


def _create_land_record_attempt(sb: Sandbox):
    return sb.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane",
        "L4",
        "--screen-result",
        "required-checks=pass",
        role="harness",
    )


def _create_land_record(sb: Sandbox) -> str:
    created = _create_land_record_attempt(sb)
    assert created.returncode == 0, created.stderr
    record_id = sorted((sb.root / "tier1/merge-records").glob("MR-*.json"))[-1].stem
    screened = transcribe_engine_screen(sb, record_id)
    assert screened.returncode == 0, screened.stderr
    finalize_gate_decision(sb, record_id)
    return record_id


def _merge(sb: Sandbox, record_id: str):
    return sb.run(
        "node",
        "merge",
        "--tree",
        "L4",
        "--node",
        "1",
        "--merge-record",
        record_id,
        role="director",
    )


def _stage_merge_cohort(sb: Sandbox, record_id: str) -> None:
    node_rel = "trees/L4/nodes/1/node.json"
    tree_rel = "trees/L4/tree.json"
    record_rel = f"tier1/merge-records/{record_id}.json"
    node = sb.load(node_rel)
    tree = sb.load(tree_rel)
    record = sb.load(record_rel)
    epoch = tree["epoch"] + 1

    merged_node = {**node, "status": "merged"}
    merged_tree = {
        **tree,
        "epoch": epoch,
        "epoch_history": tree["epoch_history"]
        + [
            {
                "epoch": epoch,
                "merged_node": "1",
                "date": _common.today(),
                "user_ratified": "n/a",
            }
        ],
        "cursor": [item for item in tree["cursor"] if item["node"] != "1"],
        "decision_log": tree["decision_log"]
        + [
            _common.decision_entry(
                "merge",
                frm="worked",
                to="merged",
                target="1",
                rationale="merge to trunk (v0 mechanical subset)",
                epoch=epoch,
            )
        ],
    }
    consumed_record = {**record, "consumed_epoch": epoch}

    sb.write_file(node_rel, json.dumps(merged_node))
    sb.write_file(tree_rel, json.dumps(merged_tree))
    sb.write_file(record_rel, json.dumps(consumed_record))
    staged = sb.git("add", node_rel, tree_rel, record_rel)
    assert staged.returncode == 0, staged.stderr


@pytest.mark.parametrize("corruption", ["merge-record", "node", "tree"])
def test_hook_rejects_merge_cohort_path_document_identity_mismatch(
    tree: Sandbox,
    corruption: str,
) -> None:
    assert _grant(tree).returncode == 0
    record_id = _create_land_record(tree)
    if corruption == "merge-record":
        relative = f"tier1/merge-records/{record_id}.json"
        document = tree.load(relative)
        document["id"] = "MR-999"
    elif corruption == "node":
        relative = "trees/L4/nodes/1/node.json"
        document = tree.load(relative)
        document["id"] = "2"
    else:
        relative = "trees/L4/tree.json"
        document = tree.load(relative)
        document["component"] = "L5"
    tree.write_file(relative, json.dumps(document))
    assert tree.git("add", "--", relative).returncode == 0
    corrupted = tree.git(
        "commit", "--no-verify", "-m", f"synthetic corrupt {corruption} identity"
    )
    assert corrupted.returncode == 0, corrupted.stderr

    _stage_merge_cohort(tree, record_id)
    rejected = tree.git(
        "commit",
        "-m",
        f"synthetic hand-staged {corruption} merge cohort",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )

    assert rejected.returncode != 0
    assert "derived mechanical-write seam" in rejected.stderr


@pytest.mark.parametrize(
    "doc_type,document",
    [
        ("claim", _claim_doc()),
        ("measurement", _measurement_doc()),
    ],
)
def test_standing_class_is_required_by_evidence_schemas(
    sandbox: Sandbox, doc_type: str, document: dict
):
    document.pop("standing_class")
    with pytest.raises(HtError, match="'standing_class' is a required property"):
        schemas.validate(sandbox.root / "system/schemas", doc_type, document)


@pytest.mark.parametrize(
    "doc_type,document",
    [
        ("claim", _claim_doc(standing_class="branch")),
        ("measurement", _measurement_doc(standing_class="branch")),
    ],
)
def test_standing_class_rejects_unknown_values(
    sandbox: Sandbox, doc_type: str, document: dict
):
    with pytest.raises(HtError, match="not one of"):
        schemas.validate(sandbox.root / "system/schemas", doc_type, document)


@pytest.mark.parametrize(
    "revalidation",
    [
        {},
        {"date": "2026-07-13", "epoch": 0},
        {"date": "2026-07-13", "ref": "report#R-1"},
        {"epoch": 0, "ref": "report#R-1"},
        {"date": "2026-07-13", "epoch": -1, "ref": "report#R-1"},
        {"date": "2026-07-13", "epoch": 0, "ref": "report#R-1", "extra": True},
    ],
)
def test_claim_revalidation_shape_is_enforced(
    sandbox: Sandbox, revalidation: dict
):
    with pytest.raises(HtError):
        schemas.validate(
            sandbox.root / "system/schemas",
            "claim",
            _claim_doc(revalidation=revalidation),
        )


def test_claim_revalidation_accepts_null_or_complete_record(sandbox: Sandbox):
    schema_dir = sandbox.root / "system/schemas"
    schemas.validate(schema_dir, "claim", _claim_doc())
    schemas.validate(
        schema_dir,
        "claim",
        _claim_doc(
            revalidation={
                "date": "2026-07-13",
                "epoch": 0,
                "ref": "adjudication#A-1",
            }
        ),
    )


def test_measurements_do_not_accept_revalidation_records(sandbox: Sandbox):
    with pytest.raises(HtError, match="Additional properties are not allowed"):
        schemas.validate(
            sandbox.root / "system/schemas",
            "measurement",
            _measurement_doc(revalidation=None),
        )


def test_claim_grant_requires_standing_class_argument(tree: Sandbox):
    result = tree.run(
        "claim",
        "grant",
        "--dispatch",
        "d-1-1",
        "--text",
        "claim",
        "--proposed-tier",
        "1",
        "--granted-tier",
        "1",
        role="verifier",
    )
    assert result.returncode != 0
    assert "--standing-class" in result.stderr


def test_claim_grant_writes_declared_class_and_null_revalidation(tree: Sandbox):
    granted = _grant(tree, "slice")
    assert granted.returncode == 0, granted.stderr
    claim = tree.load("trees/L4/nodes/1/node.json")["claims"][0]
    assert claim["standing_class"] == "slice"
    assert claim["revalidation"] is None


@pytest.mark.parametrize("claim_id", ["c-1-1１", "c-1１-1"])
def test_claim_revalidate_rejects_non_ascii_digits_without_mutation(
    sandbox: Sandbox, claim_id: str
):
    before_head = sandbox.git("rev-parse", "HEAD").stdout
    assert sandbox.git("status", "--short").stdout == ""

    rejected = sandbox.run(
        "claim",
        "revalidate",
        claim_id,
        "--ref",
        "adjudication#A-1",
        role="verifier",
    )

    assert rejected.returncode != 0
    assert (
        f"malformed claim id '{claim_id}' (expected c-<node>-<N>)"
        in rejected.stderr
    )
    assert sandbox.git("rev-parse", "HEAD").stdout == before_head
    assert sandbox.git("status", "--short").stdout == ""


@pytest.mark.parametrize("status", ["superseded", "contradicted"])
def test_claim_revalidate_rejects_non_granted_claims(
    tree: Sandbox, status: str
):
    assert _grant(tree, "sandbox").returncode == 0
    node_path = tree.root / "trees/L4/nodes/1/node.json"
    node = tree.load("trees/L4/nodes/1/node.json")
    node["claims"][0]["status"] = status
    node_path.write_text(json.dumps(node))

    rejected = tree.run(
        "claim",
        "revalidate",
        "c-1-1",
        "--ref",
        "adjudication#A-1",
        role="verifier",
    )

    assert rejected.returncode != 0
    assert f"claim c-1-1 status '{status}' cannot be revalidated" in rejected.stderr
    assert "revalidation attaches to granted claims only [R-i7-3]" in rejected.stderr
    assert tree.load("trees/L4/nodes/1/node.json") == node


def test_claim_revalidate_fills_once(tree: Sandbox):
    assert _grant(tree, "sandbox").returncode == 0
    filled = tree.run(
        "claim",
        "revalidate",
        "c-1-1",
        "--ref",
        "adjudication#A-1",
        role="verifier",
    )
    assert filled.returncode == 0, filled.stderr
    claim = tree.load("trees/L4/nodes/1/node.json")["claims"][0]
    assert claim["revalidation"] == {
        "date": _common.today(),
        "epoch": 0,
        "ref": "adjudication#A-1",
    }

    replay = tree.run(
        "claim",
        "revalidate",
        "c-1-1",
        "--ref",
        "adjudication#A-2",
        role="verifier",
    )
    assert replay.returncode != 0
    assert "already set" in replay.stderr and "fill-once" in replay.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["claims"][0] == claim


def test_non_verifier_cannot_revalidate_claim(tree: Sandbox):
    assert _grant(tree).returncode == 0
    rejected = tree.run(
        "claim",
        "revalidate",
        "c-1-1",
        "--ref",
        "adjudication#A-1",
        role="director",
    )
    assert rejected.returncode != 0
    assert "node.claims" in rejected.stderr and "owner: verifier" in rejected.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["claims"][0]["revalidation"] is None


def test_claim_revalidate_rejects_future_epoch(tree: Sandbox):
    assert _grant(tree, "sandbox").returncode == 0
    rejected = tree.run(
        "claim",
        "revalidate",
        "c-1-1",
        "--ref",
        "adjudication#A-1",
        "--epoch",
        "1",
        role="verifier",
    )
    assert rejected.returncode != 0
    assert "epoch stamp 1 outside legal range 0..0" in rejected.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["claims"][0]["revalidation"] is None


def test_claim_revalidate_epoch_cannot_predate_grant_but_equality_is_legal(
    tree: Sandbox,
):
    claim_id = _grant_after_epoch_advance(tree)
    claim_path = "trees/L4/nodes/2/node.json"
    original_claim = tree.load(claim_path)["claims"][0]

    rejected = tree.run(
        "claim",
        "revalidate",
        claim_id,
        "--ref",
        "adjudication#A-early",
        "--epoch",
        "0",
        role="verifier",
    )
    assert rejected.returncode != 0
    assert "revalidation epoch 0 predates claim grant epoch 1" in rejected.stderr
    assert (
        "revalidation must not predate the claim's grant epoch "
        "(§10: re-checked against CURRENT trunk) [R-i7-3]"
        in rejected.stderr
    )
    assert tree.load(claim_path)["claims"][0] == original_claim

    accepted = tree.run(
        "claim",
        "revalidate",
        claim_id,
        "--ref",
        "adjudication#A-equal",
        "--epoch",
        "1",
        role="verifier",
    )
    assert accepted.returncode == 0, accepted.stderr
    assert tree.load(claim_path)["claims"][0]["revalidation"] == {
        "date": _common.today(),
        "epoch": 1,
        "ref": "adjudication#A-equal",
    }


@pytest.mark.parametrize("standing_class", ["sandbox", "slice"])
def test_mrec_rejects_unrevalidated_non_trunk_claim(
    tree: Sandbox, standing_class: str
):
    assert _grant(tree, standing_class).returncode == 0
    rejected = _create_land_record_attempt(tree)
    assert rejected.returncode != 0
    assert "c-1-1" in rejected.stderr
    assert "merge-ineligible granted claims" in rejected.stderr
    assert not list((tree.root / "tier1/merge-records").glob("MR-*.json"))
    assert tree.load("trees/L4/nodes/1/node.json")["status"] == "worked"


def test_merge_record_snapshot_rejects_removed_standing_class(tree: Sandbox):
    assert _grant(tree).returncode == 0
    record_id = _create_land_record(tree)
    node_path = tree.root / "trees/L4/nodes/1/node.json"
    legacy = tree.load("trees/L4/nodes/1/node.json")
    legacy["claims"][0].pop("standing_class")
    node_path.write_text(json.dumps(legacy))

    rejected = _merge(tree, record_id)
    assert rejected.returncode != 0
    assert "backing claim" in rejected.stderr and "hash drifted" in rejected.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["status"] == "worked"

    with pytest.raises(HtError, match="backing claim .* hash drifted"):
        node_cmd.merge(Ctx(Root(tree.root), "director"), "1", "L4", record_id)


def test_revalidation_unblocks_non_trunk_merge(tree: Sandbox):
    assert _grant(tree, "sandbox").returncode == 0
    revalidated = tree.run(
        "claim",
        "revalidate",
        "c-1-1",
        "--ref",
        "adjudication#A-1",
        role="verifier",
    )
    assert revalidated.returncode == 0, revalidated.stderr
    record_id = _create_land_record(tree)
    merged = _merge(tree, record_id)
    assert merged.returncode == 0, merged.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["status"] == "merged"


def test_trunk_claim_merges_without_revalidation(tree: Sandbox):
    assert _grant(tree, "trunk").returncode == 0
    record_id = _create_land_record(tree)
    merged = _merge(tree, record_id)
    assert merged.returncode == 0, merged.stderr
    claim = tree.load("trees/L4/nodes/1/node.json")["claims"][0]
    assert claim["revalidation"] is None


@pytest.mark.parametrize("revalidated", [False, True])
def test_hook_replays_standing_class_gate_on_hand_staged_merge(
    tree: Sandbox, revalidated: bool
):
    assert _grant(tree, "sandbox").returncode == 0
    filled = tree.run(
        "claim",
        "revalidate",
        "c-1-1",
        "--ref",
        "adjudication#A-1",
        role="verifier",
    )
    assert filled.returncode == 0, filled.stderr
    record_id = _create_land_record(tree)
    if not revalidated:
        node_path = tree.root / "trees/L4/nodes/1/node.json"
        node = tree.load("trees/L4/nodes/1/node.json")
        node["claims"][0]["revalidation"] = None
        node_path.write_text(json.dumps(node), encoding="utf-8")
    _stage_merge_cohort(tree, record_id)

    committed = tree.git(
        "commit",
        "-m",
        "hand-stage merge cohort",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )
    if revalidated:
        assert committed.returncode == 0, committed.stderr
        assert tree.load("trees/L4/nodes/1/node.json")["status"] == "merged"
    else:
        assert committed.returncode != 0
        assert "derived mechanical-write seam" in committed.stderr


def test_hook_rejects_future_epoch_revalidation_in_hand_staged_merge(
    tree: Sandbox,
):
    assert _grant(tree, "sandbox").returncode == 0
    filled = tree.run(
        "claim", "revalidate", "c-1-1", "--ref", "adjudication#A-1",
        role="verifier",
    )
    assert filled.returncode == 0, filled.stderr
    record_id = _create_land_record(tree)
    node_path = tree.root / "trees/L4/nodes/1/node.json"
    node = tree.load("trees/L4/nodes/1/node.json")
    node["claims"][0]["revalidation"]["epoch"] = 99
    node_path.write_text(json.dumps(node), encoding="utf-8")
    _stage_merge_cohort(tree, record_id)

    committed = tree.git(
        "commit", "-m", "hand-stage future revalidation merge",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )
    assert committed.returncode != 0
    assert "derived mechanical-write seam" in committed.stderr


def test_hook_rejects_nonnull_malformed_revalidation_in_hand_staged_merge(
    tree: Sandbox,
):
    assert _grant(tree, "sandbox").returncode == 0
    filled = tree.run(
        "claim", "revalidate", "c-1-1", "--ref", "adjudication#A-1",
        role="verifier",
    )
    assert filled.returncode == 0, filled.stderr
    record_id = _create_land_record(tree)
    node_path = tree.root / "trees/L4/nodes/1/node.json"
    node = tree.load("trees/L4/nodes/1/node.json")
    node["claims"][0]["revalidation"]["date"] = ""
    node["claims"][0]["revalidation"]["ref"] = ""
    node_path.write_text(json.dumps(node), encoding="utf-8")
    _stage_merge_cohort(tree, record_id)

    committed = tree.git(
        "commit", "-m", "hand-stage malformed revalidation merge",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )
    assert committed.returncode != 0
    assert "derived mechanical-write seam" in committed.stderr


def test_hook_rejects_cross_issue_candidate_and_backing_cohort(
    tree: Sandbox,
):
    assert _grant(tree, "trunk").returncode == 0
    record_id = _create_land_record(tree)
    node_path = tree.root / "trees/L4/nodes/1/node.json"
    dispatch_path = tree.root / "trees/L4/nodes/1/dispatches/d-1-1.json"
    adjudication_path = (
        tree.root / "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    )
    node = tree.load("trees/L4/nodes/1/node.json")
    node["minted_from"] = "issue#I-1"
    node_path.write_text(json.dumps(node), encoding="utf-8")
    dispatch = tree.load("trees/L4/nodes/1/dispatches/d-1-1.json")
    dispatch["issue_ref"] = "I-2"
    dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")
    first, body = adjudication_path.read_text(encoding="utf-8").split("\n", 1)
    header = json.loads(first.removeprefix(ADJUDICATION_HEADER_PREFIX))
    header["issue_ref"] = "issue#I-2"
    adjudication_path.write_text(
        ADJUDICATION_HEADER_PREFIX
        + json.dumps(header, sort_keys=True, separators=(",", ":"))
        + "\n"
        + body,
        encoding="utf-8",
    )
    assert tree.git(
        "add", "--", node_path.relative_to(tree.root).as_posix(),
        dispatch_path.relative_to(tree.root).as_posix(),
        adjudication_path.relative_to(tree.root).as_posix(),
    ).returncode == 0
    forged = tree.git("commit", "--no-verify", "-m", "synthetic cross-issue state")
    assert forged.returncode == 0, forged.stderr

    _stage_merge_cohort(tree, record_id)
    committed = tree.git(
        "commit", "-m", "hand-stage cross-issue merge",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"},
    )
    assert committed.returncode != 0
    assert "derived mechanical-write seam" in committed.stderr
