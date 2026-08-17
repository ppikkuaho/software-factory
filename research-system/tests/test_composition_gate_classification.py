"""Phase-A W3 tests for the shared committed-screen classifier."""

from __future__ import annotations

import json

import pytest

from composition_gate.classification import ALLGREEN, INVALID, SEMANTIC_FAILURE, classify_screen
from composition_gate.screen import CHECK_NAMES, render_screen, run_screen

from conftest import Sandbox, seed_mrec_candidate


def _create(sb: Sandbox, record_id: str, *, surface: str | None = None) -> None:
    candidate_ref = f"tree#L4/node#{record_id.removeprefix('MR-')}"
    adjudication_ref = seed_mrec_candidate(sb, candidate_ref)
    args = [
        "mrec",
        "create",
        "--candidate-ref",
        candidate_ref,
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "L4",
    ]
    if surface is not None:
        args.extend(["--scope-surface", surface])
    created = sb.run(*args, role="harness")
    assert created.returncode == 0, created.stderr


def _scope(lane: str, *, surfaces: list[str] | None = None) -> dict:
    return {"lane": lane, "seats": [], "surfaces": surfaces or [], "globs": []}


def _raw_record(
    record_id: str,
    lane: str,
    *,
    surfaces: list[str] | None = None,
    consumed_epoch: int | None = None,
    gate_verdict: dict | None = None,
) -> dict:
    ordinal = record_id.removeprefix("MR-")
    return {
        "id": record_id,
        "candidate_ref": f"tree#{lane}/node#{ordinal}",
        "lane_verdict": "lane-pass",
        "scope": _scope(lane, surfaces=surfaces),
        "screen": {"results": [], "log_ref": None},
        "gate_verdict": gate_verdict,
        "watch_link": None,
        "created": "2026-07-13",
        "consumed_epoch": consumed_epoch,
    }


def _raw_tree(component: str, watch_queue: list[dict]) -> dict:
    return {
        "component": component,
        "root_question": f"Synthetic question for {component}",
        "epoch": 0,
        "epoch_history": [],
        "cursor": [],
        "decision_log": [],
        "global_learnings": [],
        "watch_queue": watch_queue,
    }


def _commit_documents(sb: Sandbox, documents: dict[str, dict], message: str) -> None:
    for relative, document in documents.items():
        sb.write_file(relative, json.dumps(document, indent=2) + "\n")
    added = sb.git("add", "--", *documents)
    assert added.returncode == 0, added.stderr
    committed = sb.git("commit", "--no-verify", "-m", message)
    assert committed.returncode == 0, committed.stderr


def _watch(row_id: str, merged_node: str, epoch: int) -> dict:
    return {
        "id": row_id,
        "merged_node": merged_node,
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": epoch,
    }


def _transcribe(sb: Sandbox, record_id: str, payload: dict | None = None) -> None:
    output = sb.root / f"var/{record_id}-screen.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        payload = run_screen(sb.root, record_id)
    output.write_text(render_screen(payload), encoding="utf-8")
    log = sb.root / f"var/{record_id}-log.json"
    log.write_bytes(output.read_bytes())
    screened = sb.run(
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        f"var/{record_id}-log.json",
        role="harness",
    )
    assert screened.returncode == 0, screened.stderr


def test_classifier_returns_allgreen_only_for_a_rehashed_engine_screen(
    sandbox: Sandbox,
):
    _create(sandbox, "MR-1")
    _transcribe(sandbox, "MR-1")
    record = sandbox.load("tier1/merge-records/MR-1.json")
    assert classify_screen(sandbox.root, record) == ALLGREEN


def test_public_classification_vocabulary_matches_the_frozen_contract():
    assert (INVALID, ALLGREEN, SEMANTIC_FAILURE) == (
        "invalid",
        "allgreen",
        "semantic-failure",
    )


def test_semantic_failure_is_valid_but_not_allgreen_and_exact_land_is_refused(
    sandbox: Sandbox,
):
    _create(sandbox, "MR-1", surface="panel")
    _create(sandbox, "MR-2", surface="panel")
    _transcribe(sandbox, "MR-1")
    record = sandbox.load("tier1/merge-records/MR-1.json")
    assert classify_screen(sandbox.root, record) == SEMANTIC_FAILURE

    rejected = sandbox.run(
        "mrec",
        "verdict",
        "--record",
        "MR-1",
        "--verdict",
        "land",
        role="cgate",
    )
    assert rejected.returncode != 0
    assert "[R-i7-9" in rejected.stderr
    assert record["gate_verdict"] is None


def test_forged_pass_with_nonempty_inputs_is_invalid(sandbox: Sandbox):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    payload["results"][0]["inputs"]["config"]["name"] = "forged-config.json"
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


def test_hash_mismatch_and_contradictory_outcome_are_invalid(
    sandbox: Sandbox,
):
    _create(sandbox, "MR-1")
    _transcribe(sandbox, "MR-1")
    output = sandbox.root / "var/MR-1-screen.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["results"][0]["result"] = "fail"
    output.write_text(render_screen(payload), encoding="utf-8")
    record = sandbox.load("tier1/merge-records/MR-1.json")
    assert classify_screen(sandbox.root, record) == INVALID


def test_log_hash_mismatch_is_invalid(sandbox: Sandbox):
    _create(sandbox, "MR-1")
    _transcribe(sandbox, "MR-1")
    (sandbox.root / "var/MR-1-log.json").write_text("mutated synthetic log\n")
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


def test_screen_error_evidence_is_invalid(sandbox: Sandbox):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    payload["results"][0]["detail"] = "screen-error: synthetic failure"
    payload["results"][0]["inputs"]["error"] = {
        "type": "SyntheticError",
        "message": "fixture",
    }
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


@pytest.mark.parametrize("check", CHECK_NAMES)
def test_each_check_rejects_an_outcome_that_contradicts_its_inputs(
    sandbox: Sandbox,
    check: str,
):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    row = next(item for item in payload["results"] if item["check"] == check)
    assert row["result"] != "fail"
    row["result"] = "fail"
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


@pytest.mark.parametrize(
    ("check", "illegal_result"),
    [
        ("scope-overlap", "n/a"),
        ("surface-budget", "pass"),
        ("settlement-completeness", "n/a"),
        ("queue-adjacency", "n/a"),
        ("watch-debt", "pass"),
    ],
)
def test_each_check_rejects_its_check_specific_illegal_na_or_pass(
    sandbox: Sandbox,
    check: str,
    illegal_result: str,
):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    row = next(item for item in payload["results"] if item["check"] == check)
    row["result"] = illegal_result
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


@pytest.mark.parametrize(
    ("check", "required_field"),
    [
        ("scope-overlap", "comparisons"),
        ("surface-budget", "surface_counts"),
        ("settlement-completeness", "overdue_rows"),
        ("queue-adjacency", "pending_count"),
        ("watch-debt", "overlapping_watch_rows"),
    ],
)
def test_each_check_rejects_a_missing_required_semantic_field(
    sandbox: Sandbox,
    check: str,
    required_field: str,
):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    row = next(item for item in payload["results"] if item["check"] == check)
    row["inputs"].pop(required_field)
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


@pytest.mark.parametrize("check", CHECK_NAMES)
def test_each_check_requires_its_exact_successful_store_citations(
    sandbox: Sandbox,
    check: str,
):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    row = next(item for item in payload["results"] if item["check"] == check)
    row["inputs"]["discovery"]["stores"].popitem()
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


@pytest.mark.parametrize(
    "check",
    CHECK_NAMES,
)
def test_each_check_rejects_summary_or_row_cross_reference_contradictions(
    sandbox: Sandbox,
    check: str,
):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    inputs = next(
        item["inputs"] for item in payload["results"] if item["check"] == check
    )
    if check == "scope-overlap":
        inputs["collisions"] = [
            {"record_id": "MR-9", "axes": ["lane"], "values": {"lane": "L4"}}
        ]
    elif check == "surface-budget":
        inputs["surface_counts"] = {"ghost": {"count": 0, "records": []}}
    elif check == "settlement-completeness":
        inputs["lane_frontier"] = 0
    elif check == "queue-adjacency":
        inputs["pending_count"] += 1
    else:
        inputs["overlapping_watch_rows"] = [{}]
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engine_version", "composition-gate/forged"),
        ("config_hash", "0" * 64),
        ("head_tree", "0" * 40),
        ("head_commit", None),
    ],
)
def test_classifier_rejects_forged_engine_and_git_identity(
    sandbox: Sandbox,
    field: str,
    value,
):
    _create(sandbox, "MR-1")
    payload = run_screen(sandbox.root, "MR-1")
    payload[field] = value
    if field == "head_commit":
        payload["head_tree"] = None
    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


def test_classifier_accepts_surface_budget_failure_as_semantic_failure(
    sandbox: Sandbox,
):
    documents = {
        "tier1/merge-records/MR-20.json": _raw_record(
            "MR-20", "candidate", surfaces=["merge-queue"]
        )
    }
    for ordinal in range(1, 12):
        record_id = f"MR-{ordinal}"
        documents[f"tier1/merge-records/{record_id}.json"] = _raw_record(
            record_id,
            f"lane-{ordinal}",
            surfaces=["merge-queue"],
            consumed_epoch=ordinal,
            gate_verdict={"verdict": "land", "date": "2026-07-13"},
        )
    _commit_documents(sandbox, documents, "seed synthetic surface budget")
    _transcribe(sandbox, "MR-20")
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-20.json")
    ) == SEMANTIC_FAILURE


def test_classifier_accepts_settlement_failure_as_semantic_failure(
    sandbox: Sandbox,
):
    source = _raw_record("MR-1", "other", consumed_epoch=4, gate_verdict={"verdict": "land", "date": "2026-07-13"})
    frontier = _raw_record("MR-2", "candidate", consumed_epoch=5, gate_verdict={"verdict": "land", "date": "2026-07-13"})
    candidate = _raw_record("MR-3", "candidate")
    queue = [
        _watch("W-1", source["candidate_ref"], 4),
        _watch("W-2", frontier["candidate_ref"], 5),
    ]
    _commit_documents(
        sandbox,
        {
            "tier1/merge-records/MR-1.json": source,
            "tier1/merge-records/MR-2.json": frontier,
            "tier1/merge-records/MR-3.json": candidate,
            "trees/observer/tree.json": _raw_tree("observer", queue),
        },
        "seed synthetic settlement debt",
    )
    _transcribe(sandbox, "MR-3")
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-3.json")
    ) == SEMANTIC_FAILURE


def test_classifier_accepts_queue_adjacency_failure_as_semantic_failure(
    sandbox: Sandbox,
):
    _commit_documents(
        sandbox,
        {
            "tier1/merge-records/MR-1.json": _raw_record(
                "MR-1", "candidate", surfaces=["shared"]
            ),
            "tier1/merge-records/MR-2.json": _raw_record(
                "MR-2", "pending", surfaces=["shared"]
            ),
            "tier1/merge-records/MR-3.json": _raw_record(
                "MR-3",
                "pending-land",
                gate_verdict={"verdict": "land", "date": "2026-07-13"},
            ),
        },
        "seed synthetic adjacency debt",
    )
    _transcribe(sandbox, "MR-1")
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == SEMANTIC_FAILURE


def test_classifier_rejects_forged_omission_of_committed_pending_frontier(
    sandbox: Sandbox,
):
    """A self-consistent subset is not the committed W2 discovery snapshot."""

    _commit_documents(
        sandbox,
        {
            f"tier1/merge-records/MR-{ordinal}.json": _raw_record(
                f"MR-{ordinal}", "shared-lane", surfaces=["shared-surface"]
            )
            for ordinal in range(1, 4)
        },
        "seed synthetic omitted-frontier attack",
    )
    payload = run_screen(sandbox.root, "MR-1")
    scope = next(
        row for row in payload["results"] if row["check"] == "scope-overlap"
    )
    queue = next(
        row for row in payload["results"] if row["check"] == "queue-adjacency"
    )
    assert scope["result"] == queue["result"] == "fail"

    scope["result"] = "pass"
    scope["detail"] = "forged self-consistent candidate-only scope"
    scope["inputs"]["pending_record_ids"] = []
    scope["inputs"]["comparisons"] = []
    scope["inputs"]["collisions"] = []
    queue["result"] = "pass"
    queue["detail"] = "forged self-consistent candidate-only queue"
    queue["inputs"]["pending_records"] = [queue["inputs"]["candidate"]]
    queue["inputs"]["pending_count"] = 1
    queue["inputs"]["shared_surfaces"] = []

    _transcribe(sandbox, "MR-1", payload)
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    ) == INVALID


def test_classifier_accepts_watch_debt_failure_as_semantic_failure(
    sandbox: Sandbox,
):
    source = _raw_record(
        "MR-1",
        "source",
        surfaces=["control-panel"],
        consumed_epoch=3,
        gate_verdict={"verdict": "land", "date": "2026-07-13"},
    )
    candidate = _raw_record("MR-2", "candidate-a", surfaces=["control-panel"])
    _commit_documents(
        sandbox,
        {
            "tier1/merge-records/MR-1.json": source,
            "tier1/merge-records/MR-2.json": candidate,
            "trees/observer/tree.json": _raw_tree(
                "observer", [_watch("W-1", source["candidate_ref"], 3)]
            ),
        },
        "seed synthetic watch debt",
    )
    _transcribe(sandbox, "MR-2")
    assert classify_screen(
        sandbox.root, sandbox.load("tier1/merge-records/MR-2.json")
    ) == SEMANTIC_FAILURE
