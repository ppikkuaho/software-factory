"""Pure CG-P4 decisions over the committed W2 screen classifier."""

from __future__ import annotations

import pytest

from composition_gate import rules
from composition_gate.classification import SEMANTIC_FAILURE
from composition_gate.screen import render_screen, run_screen
from conftest import Sandbox, seed_mrec_candidate


def _result(check: str, result: str, inputs: dict | None = None) -> dict:
    return {
        "check": check,
        "result": result,
        "detail": f"synthetic {check} {result}",
        "inputs": inputs or {},
    }


def _semantic_record(failed: str, inputs: dict) -> dict:
    return {
        "id": "MR-50",
        "screen": {
            "results": [
                _result(check, "fail" if check == failed else "pass", inputs if check == failed else {})
                for check in (
                    "scope-overlap",
                    "surface-budget",
                    "settlement-completeness",
                    "queue-adjacency",
                    "watch-debt",
                )
            ]
        },
    }


def _record_input(
    record_id: str,
    *,
    epoch: int | None,
    surfaces: list[str] | None = None,
    globs: list[str] | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "consumed_epoch": epoch,
        "scope": {
            "lane": record_id,
            "seats": [],
            "surfaces": surfaces or [],
            "globs": globs or [],
        },
    }


def _overlap_inputs() -> dict:
    candidate = _record_input(
        "MR-50",
        epoch=None,
        surfaces=["candidate-surface"],
        globs=["src/*.py"],
    )
    consumed_10 = _record_input("MR-10", epoch=1)
    consumed_2 = _record_input("MR-2", epoch=1)
    pending = _record_input(
        "MR-60",
        epoch=None,
        surfaces=["other-surface"],
        globs=["docs/*.md"],
    )
    return {
        "candidate": candidate,
        "collisions": [
            {"record_id": "MR-10", "axes": ["lane"], "values": {"lane": "L4"}},
            {"record_id": "MR-2", "axes": ["seats"], "values": {"seats": ["senior"]}},
        ],
        "comparisons": [
            {
                "record": consumed_10,
                "comparison_sets": ["last-consumed"],
                "collision": {"axes": ["lane"], "values": {"lane": "L4"}},
            },
            {
                "record": consumed_2,
                "comparison_sets": ["last-consumed"],
                "collision": {"axes": ["seats"], "values": {"seats": ["senior"]}},
            },
            {
                "record": pending,
                "comparison_sets": ["pending"],
                "collision": {"axes": [], "values": {}},
            },
        ],
    }


@pytest.fixture
def semantic_classifier(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rules, "classify_screen", lambda _root, _record: SEMANTIC_FAILURE)


def test_invalid_and_allgreen_priorities_return_exact_decision_shape(
    monkeypatch: pytest.MonkeyPatch,
):
    record = {"id": "MR-1", "screen": {"results": []}}
    monkeypatch.setattr(rules, "classify_screen", lambda _root, _record: "invalid")
    invalid = rules.evaluate_rules("/unused", record)
    assert invalid == {
        "route": "stuck",
        "verdict": "escalate-stuck",
        "note": "Committed W2 screen evidence is invalid or cannot be revalidated.",
        "rules_fired": [
            {"rule_id": "R-SCREEN-INVALID", "outcome": "escalate-stuck"}
        ],
    }
    assert set(invalid) == {"route", "verdict", "note", "rules_fired"}

    monkeypatch.setattr(rules, "classify_screen", lambda _root, _record: "allgreen")
    assert rules.evaluate_rules("/unused", record) == {
        "route": "auto",
        "verdict": "land",
        "note": "All five checks passed or returned legitimate n/a.",
        "rules_fired": [{"rule_id": "R-ALLGREEN", "outcome": "land"}],
    }


def test_overlap_sequence_joins_engine_record_ids_and_uses_numeric_oldest(
    semantic_classifier,
):
    decision = rules.evaluate_rules(
        "/unused", _semantic_record("scope-overlap", _overlap_inputs())
    )
    assert decision["route"] == "stage2"
    assert decision["verdict"] is None
    assert decision["rules_fired"] == [
        {"rule_id": "R-OVERLAP-SEQ", "outcome": "land-after-MR-2"}
    ]
    assert "MR-2" in decision["note"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda inputs: inputs["comparisons"].pop(0),
        lambda inputs: inputs["comparisons"].append(inputs["comparisons"][0]),
        lambda inputs: inputs["comparisons"][0].update(
            {"comparison_sets": ["last-consumed", "pending"]}
        ),
        lambda inputs: inputs["collisions"][0].update(
            {"axes": ["surfaces"], "values": {"surfaces": ["shared"]}}
        ),
        lambda inputs: inputs["comparisons"][-1]["record"]["scope"].update(
            {"surfaces": ["candidate-surface"]}
        ),
        lambda inputs: inputs["comparisons"][-1]["record"]["scope"].update(
            {"globs": ["src/**"]}
        ),
    ],
)
def test_overlap_missing_duplicate_or_ambiguous_fields_fall_through_to_stage2(
    semantic_classifier,
    mutate,
):
    inputs = _overlap_inputs()
    mutate(inputs)
    decision = rules.evaluate_rules(
        "/unused", _semantic_record("scope-overlap", inputs)
    )
    assert decision == {
        "route": "stage2",
        "verdict": None,
        "note": "Valid semantic failure requires stage-2 review.",
        "rules_fired": [],
    }


def test_settlement_hold_note_names_every_overdue_row_deterministically(
    semantic_classifier,
):
    inputs = {
        "overdue_rows": [
            {"tree_path": "trees/L5/tree.json", "row_id": "watch-10"},
            {"tree_path": "trees/L4/tree.json", "row_id": "watch-2"},
        ]
    }
    decision = rules.evaluate_rules(
        "/unused", _semantic_record("settlement-completeness", inputs)
    )
    assert decision["rules_fired"] == [
        {"rule_id": "R-SETTLE-HOLD", "outcome": "hold"}
    ]
    assert decision["note"].endswith(
        "trees/L4/tree.json#watch-2, trees/L5/tree.json#watch-10."
    )


def test_settlement_missing_overdue_rows_falls_through(semantic_classifier):
    decision = rules.evaluate_rules(
        "/unused",
        _semantic_record("settlement-completeness", {"overdue_rows": []}),
    )
    assert decision["route"] == "stage2"
    assert decision["rules_fired"] == []


def test_consolidate_note_lists_pending_ids_in_canonical_numeric_order(
    semantic_classifier,
):
    inputs = {
        "pending_count": 3,
        "pending_records": [
            {"record_id": "MR-10"},
            {"record_id": "MR-2"},
            {"record_id": "MR-1"},
        ],
    }
    decision = rules.evaluate_rules(
        "/unused", _semantic_record("queue-adjacency", inputs)
    )
    assert decision["rules_fired"] == [
        {"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"}
    ]
    assert decision["note"].endswith("MR-1, MR-2, MR-10.")


@pytest.mark.parametrize(
    "inputs",
    [
        {"pending_count": 2, "pending_records": [{"record_id": "MR-1"}]},
        {
            "pending_count": 3,
            "pending_records": [
                {"record_id": "MR-1"},
                {"record_id": "MR-1"},
                {"record_id": "MR-2"},
            ],
        },
        {"pending_count": 3, "pending_records": [{"id": "MR-1"}]},
    ],
)
def test_consolidate_missing_or_ambiguous_pending_fields_do_not_fire(
    semantic_classifier,
    inputs: dict,
):
    decision = rules.evaluate_rules(
        "/unused", _semantic_record("queue-adjacency", inputs)
    )
    assert decision["route"] == "stage2"
    assert decision["rules_fired"] == []


@pytest.mark.parametrize("failed", ["surface-budget", "watch-debt"])
def test_budget_and_watch_semantic_failures_have_no_stage1_preset(
    semantic_classifier,
    failed: str,
):
    decision = rules.evaluate_rules("/unused", _semantic_record(failed, {}))
    assert decision == {
        "route": "stage2",
        "verdict": None,
        "note": "Valid semantic failure requires stage-2 review.",
        "rules_fired": [],
    }


def test_multiple_semantic_failures_never_fire_a_sole_failure_preset(
    semantic_classifier,
):
    record = _semantic_record("scope-overlap", _overlap_inputs())
    record["screen"]["results"][2]["result"] = "fail"
    decision = rules.evaluate_rules("/unused", record)
    assert decision["route"] == "stage2"
    assert decision["verdict"] is None
    assert decision["rules_fired"] == []


def _create_pending(sb: Sandbox, lane: str) -> None:
    candidate_ref = f"tree#{lane}/node#1"
    adjudication_ref = seed_mrec_candidate(sb, candidate_ref)
    created = sb.run(
        "mrec",
        "create",
        "--candidate-ref",
        candidate_ref,
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        lane,
        role="harness",
    )
    assert created.returncode == 0, created.stderr


def test_rules_use_the_real_committed_classifier_for_all_three_routes(
    sandbox: Sandbox,
):
    _create_pending(sandbox, "L1")
    invalid = rules.evaluate_rules(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    )
    assert invalid["route"] == "stuck"

    output = sandbox.root / "var/MR-1-screen.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_screen(run_screen(sandbox.root, "MR-1")), encoding="utf-8")
    log = sandbox.root / "var/MR-1-log.json"
    log.write_bytes(output.read_bytes())
    screened = sandbox.run(
        "mrec",
        "screen",
        "MR-1",
        "--results-json",
        str(output),
        "--log-ref",
        "var/MR-1-log.json",
        role="harness",
    )
    assert screened.returncode == 0, screened.stderr
    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    status_before = sandbox.git("status", "--porcelain").stdout
    evidence_before = (output.read_bytes(), log.read_bytes())
    assert rules.evaluate_rules(
        sandbox.root, sandbox.load("tier1/merge-records/MR-1.json")
    )["route"] == "auto"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert sandbox.git("status", "--porcelain").stdout == status_before
    assert (output.read_bytes(), log.read_bytes()) == evidence_before

    _create_pending(sandbox, "L2")
    _create_pending(sandbox, "L3")
    output = sandbox.root / "var/MR-3-screen.json"
    output.write_text(render_screen(run_screen(sandbox.root, "MR-3")), encoding="utf-8")
    log = sandbox.root / "var/MR-3-log.json"
    log.write_bytes(output.read_bytes())
    screened = sandbox.run(
        "mrec",
        "screen",
        "MR-3",
        "--results-json",
        str(output),
        "--log-ref",
        "var/MR-3-log.json",
        role="harness",
    )
    assert screened.returncode == 0, screened.stderr
    semantic = rules.evaluate_rules(
        sandbox.root, sandbox.load("tier1/merge-records/MR-3.json")
    )
    assert semantic["route"] == "stage2"
    assert semantic["rules_fired"] == [
        {"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"}
    ]
    assert semantic["note"].endswith("MR-1, MR-2, MR-3.")
