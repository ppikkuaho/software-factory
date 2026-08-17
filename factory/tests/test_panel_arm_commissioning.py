"""Run-5 panel-arm commissioning: one start parameter, one review-dispatch spine."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessd import addressing, ledger, review_dispatch
from harnessd.spawn import chokepoint


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def _review_pair(
    runtime: Path,
    *,
    producer_address: str,
    axes: list[str],
    gate_id: str | None = None,
) -> tuple[str, dict, dict]:
    review_address = producer_address.replace("#exec", "#review")
    gate_id = gate_id or "gate-" + producer_address.split("/")[-1].split("#", 1)[0]
    gate_dir = addressing.node_dir(producer_address, runtime) / "reviews" / gate_id
    gate_dir.mkdir(parents=True, exist_ok=True)
    packet = gate_dir / "review-packet.md"
    packet.write_text("# Review packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# Review Plan\n\n"
        "Review Mode: FULL\n\n"
        "## Role Selection\n\n"
        f"Selected configured module panel: {', '.join(axes)}.\n",
        encoding="utf-8",
    )
    producer = {
        "node_address": producer_address,
        "parent_address": "forge-queue#exec",
        "level": "L3",
        "state": "running",
        "gate_state": "candidate_submitted",
        "gate_id": gate_id,
        "gate_review_dir": str(gate_dir),
        "gate_review_packet": str(packet),
    }
    review = {
        "node_address": review_address,
        "parent_address": "forge-queue#exec",
        "level": "L3+",
        "role_variant": "L3+#review",
        "state": "running",
        "gate_for": producer_address,
    }
    return review_address, producer, review


def _commissioned_contract_fixture(
    runtime: Path,
    *,
    producer_address: str,
    axes: list[str],
    gate_id: str,
) -> tuple[str, dict, Path, dict[str, Path]]:
    """Materialize the live one/two/four-seat FULL contract shape."""
    root = {
        "node_address": "L1#exec",
        "parent_address": None,
        "level": "L1",
        "state": "running",
        "review_panel_arms": [
            {
                "pattern": producer_address,
                "axes": list(axes),
            }
        ],
    }
    review_address, producer, review = _review_pair(
        runtime,
        producer_address=producer_address,
        axes=axes,
        gate_id=gate_id,
    )
    ledger.write_binding(
        {
            root["node_address"]: root,
            producer["node_address"]: producer,
            review_address: review,
        },
        _lock_held=True,
    )

    gate_dir = Path(producer["gate_review_dir"])
    live = ledger.all_nodes()
    reports: dict[str, Path] = {}
    producer_path, _seat = addressing.split_address(producer_address)
    for spec in review_dispatch.required_review_check_specs(review):
        slug = str(spec["slug"])
        report = review_dispatch.review_check_report_path(gate_dir, spec)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            f"# {spec['label']}\n\n"
            "Recommended Routing: accept-note\n\n"
            "No material finding in the commissioned lane.\n",
            encoding="utf-8",
        )
        check_address = (
            f"{producer_path}/reviews/{gate_id}/reviewers/{slug}#exec"
        )
        live[check_address] = {
            "node_address": check_address,
            "parent_address": review_address,
            "level": "L3+",
            "state": "done",
            "review_check_for": review_address,
            "review_check_candidate": producer_address,
            "gate_id": gate_id,
            "review_check_axis": slug,
            "review_check_report": str(report),
        }
        reports[slug] = report
    ledger.write_binding(live, _lock_held=True)
    return review_address, review, gate_dir, reports


def test_unconfigured_l3_panel_is_the_exact_current_four_axis_roster():
    assert [
        spec["slug"] for spec in review_dispatch.required_review_check_specs("L3+")
    ] == [
        "fidelity-coverage",
        "composition-interface",
        "evidence-credibility",
        "risk-readiness",
    ]
    assert review_dispatch.required_check_report_names("L3+") == (
        "reviewers/fidelity-coverage/report.md",
        "reviewers/composition-interface/report.md",
        "reviewers/evidence-credibility/report.md",
        "reviewers/risk-readiness/report.md",
    )


def test_one_run_dispatches_two_scoped_module_and_one_broad_module_in_order(
    runtime, monkeypatch
):
    root = {
        "node_address": "L1#exec",
        "parent_address": None,
        "level": "L1",
        "state": "running",
        "review_panel_arms": [
            {
                "pattern": "forge-queue/queue-core#exec",
                "axes": ["composition-interface", "risk-readiness"],
            },
            {
                "pattern": "forge-queue/persistence#exec",
                "axes": ["broad"],
            },
        ],
    }
    queue_review, queue_producer, queue_binding = _review_pair(
        runtime,
        producer_address="forge-queue/queue-core#exec",
        axes=["composition-interface", "risk-readiness"],
    )
    persistence_review, persistence_producer, persistence_binding = _review_pair(
        runtime,
        producer_address="forge-queue/persistence#exec",
        axes=["broad"],
    )
    ledger.write_binding(
        {
            root["node_address"]: root,
            queue_producer["node_address"]: queue_producer,
            queue_review: queue_binding,
            persistence_producer["node_address"]: persistence_producer,
            persistence_review: persistence_binding,
        },
        _lock_held=True,
    )
    opened = []

    def open_planned(address, **_kwargs):
        opened.append(address)
        return SimpleNamespace(ok=True, failure_class=None)

    monkeypatch.setattr(chokepoint, "claim_and_spawn", open_planned)

    queue_results = chokepoint.dispatch_review_check_seats(queue_review)
    persistence_results = chokepoint.dispatch_review_check_seats(persistence_review)

    assert len(queue_results) == 2
    assert len(persistence_results) == 1
    assert opened == [
        "forge-queue/queue-core/reviews/gate-queue-core/reviewers/"
        "composition-interface#exec",
        "forge-queue/queue-core/reviews/gate-queue-core/reviewers/"
        "risk-readiness#exec",
        "forge-queue/persistence/reviews/gate-persistence/reviewers/broad#exec",
    ]
    live = ledger.all_nodes()
    assert [
        live[address]["review_check_axis"] for address in opened
    ] == ["composition-interface", "risk-readiness", "broad"]


def test_first_matching_module_pattern_wins_in_declaration_order(runtime):
    root = {
        "node_address": "L1#exec",
        "parent_address": None,
        "level": "L1",
        "state": "running",
        "review_panel_arms": [
            {
                "pattern": "forge-queue/*#exec",
                "axes": ["risk-readiness"],
            },
            {
                "pattern": "forge-queue/queue-core#exec",
                "axes": ["broad"],
            },
        ],
    }
    review_address, producer, review = _review_pair(
        runtime,
        producer_address="forge-queue/queue-core#exec",
        axes=["risk-readiness"],
    )
    ledger.write_binding(
        {
            root["node_address"]: root,
            producer["node_address"]: producer,
            review_address: review,
        },
        _lock_held=True,
    )

    assert [
        spec["slug"] for spec in review_dispatch.required_review_check_specs(review)
    ] == ["risk-readiness"]


def test_broad_check_renders_from_its_single_unscoped_registry_source(runtime):
    root = {
        "node_address": "L1#exec",
        "parent_address": None,
        "level": "L1",
        "state": "running",
        "review_panel_arms": [
            {
                "pattern": "forge-queue/persistence#exec",
                "axes": ["broad"],
            }
        ],
    }
    review_address, producer, review = _review_pair(
        runtime,
        producer_address="forge-queue/persistence#exec",
        axes=["broad"],
    )
    ledger.write_binding(
        {
            root["node_address"]: root,
            producer["node_address"]: producer,
            review_address: review,
        },
        _lock_held=True,
    )

    specs = review_dispatch.required_review_check_specs(review)
    assert [spec["slug"] for spec in specs] == ["broad"]
    text = review_dispatch.render_review_check_brief(
        review_address=review_address,
        review_binding=review,
        producer_address=producer["node_address"],
        gate_id=producer["gate_id"],
        gate_dir=Path(producer["gate_review_dir"]),
        spec=specs[0],
    )

    task = (
        "Review the candidate for anything that would make accepting it wrong. "
        "Work without an axis lane. Follow the same evidence, report, attribution, "
        "and sign-off duties as every review-check seat."
    )
    assert task in text
    assert "reviewers/broad/report.md" in text
    assert "Stay inside this check's scope" not in text
    assert "evidence pointers" in text
    assert "do not sign ACCEPT/BOUNCE/ESCALATE" in text


@pytest.mark.parametrize(
    ("producer_address", "axes", "gate_id"),
    [
        (
            "forge-queue/wire-protocol#exec",
            ["broad"],
            "gate-3a422bff1199d840",
        ),
        (
            "forge-queue/persistence#exec",
            ["composition-interface", "risk-readiness"],
            "gate-5b763c574c0971bf",
        ),
        (
            "forge-queue/queue-core#exec",
            [
                "fidelity-coverage",
                "composition-interface",
                "evidence-credibility",
                "risk-readiness",
            ],
            "gate-4e2cc6a3eaeda03c",
        ),
    ],
)
def test_full_return_contract_uses_exact_commissioned_report_roster(
    runtime,
    producer_address,
    axes,
    gate_id,
):
    review_address, review, _gate_dir, _reports = _commissioned_contract_fixture(
        runtime,
        producer_address=producer_address,
        axes=axes,
        gate_id=gate_id,
    )

    defects = review_dispatch.dispatch_contract_defects(review_address, review)

    assert defects == []


def test_full_return_contract_still_refuses_missing_commissioned_report(runtime):
    review_address, review, _gate_dir, reports = _commissioned_contract_fixture(
        runtime,
        producer_address="forge-queue/wire-protocol#exec",
        axes=["broad"],
        gate_id="gate-3a422bff1199d840",
    )
    missing = reports["broad"]
    missing.unlink()

    defects = review_dispatch.dispatch_contract_defects(review_address, review)

    assert defects == [
        f"MISSING-REVIEWER-REPORT: {review_address} full review mode requires {missing}"
    ]
