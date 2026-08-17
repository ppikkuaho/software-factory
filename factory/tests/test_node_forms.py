"""#30 — pre-instantiated node forms (user mental model, 2026-06-12: "they get everything in
front of them" — the unfailable-convention machine, deterministically).

The chokepoint drops INSTANTIATED report.md + plan.md skeletons into every child node at
brief-write time: header pre-filled from the address, the GIVEN requirement IDs pre-listed
(derived from brief.md/acceptance.md — the same sources the E2 walker reads), the L5+ seat
getting the verified-not-discharged variant. Filling the form is the only path; zero
transcription ambiguity.

THE GATE STAYS HONEST: a skeleton would otherwise auto-satisfy E2's MISSING-REPORT (file
exists, non-empty) AND the citation check (IDs pre-filled) — so every instantiated form
carries the ``form:unfilled`` sentinel, and the walker refuses a DONE whose report still
carries it (UNFILLED-REPORT-FORM). Mutants: no instantiation -> forms absent -> caught;
sentinel dropped -> skeleton passes E2 -> caught; overwrite of existing first-spawn
parent-authored forms -> caught. A later terminal same-address incarnation is a fresh
actor surface and is covered by outbox/chokepoint regressions.
"""

import copy
import json
import shutil

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.review_dispatch as review_dispatch
import harnessd.spawn.chokepoint as chokepoint
from harnessd.spawn.adapters.base import SpawnResult

LEAF = "proj/widget/task#exec"
PARENT = "proj/widget#exec"


def _write_canonical_full_review_reports(gate_dir):
    for name in review_dispatch.required_check_report_names("L4+"):
        path = gate_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {name}\n\nRecommended Routing: accept-note\n\nNo material finding.\n",
            encoding="utf-8",
        )


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


@pytest.fixture(autouse=True)
def _reset_chokepoint_adapter():
    previous = chokepoint.ADAPTER
    try:
        yield
    finally:
        chokepoint.set_adapter(previous)


class _Tmux:
    def kill(self, target):
        pass

    def send_keys(self, target, text):
        return True

    def capture_pane(self, target):
        from harnessd import watchdog
        return f"{watchdog.FORK_PROMPT} \n? for shortcuts"


class FakeAdapter:
    def __init__(self):
        self.tmux = _Tmux()

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        return SpawnResult(
            ok=True,
            session_uuid="sess-forms-0001",
            model_used="m / r",
            role_variant=getattr(level_config, "role_variant", "L5"),
            system_prompt_file="x",
            system_prompt_file_hash="h",
            tmux_target=addressing.session_name_for(tmux_target) + ":0.0",
            transcript_path="/tmp/sess-forms-0001.jsonl",
            failure_class=None,
        )


def _seed_parent():
    token = fencing.mint_owner_token(PARENT, "sa", "uuid", 1)
    ws = addressing.node_dir(PARENT, ledger.RUNTIME_ROOT)
    ws.mkdir(parents=True, exist_ok=True)
    rec = {
        "node_address": PARENT, "parent_address": None, "level": "L4",
        "subagent_id": "sa", "session_uuid": "uuid", "state": "running",
        "generation": 1, "lease_epoch": 1, "owner_token": token,
        "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md",
        "frozen_acceptance_ref": "acceptance.md", "liveness_state": "working",
        "terminal_signal": None, "terminal_signal_at": None, "gate_crossed_at": None,
        "paused_at": None, "transcript_path": None, "workspace": str(ws),
        "tmux_target": addressing.session_name_for(PARENT) + ":0.0",
    }
    live = dict(ledger.all_nodes())
    live[PARENT] = copy.deepcopy(rec)
    ledger.write_binding(live, _lock_held=True)


def _prepare_child_acceptance():
    d = addressing.node_dir(LEAF, ledger.RUNTIME_ROOT)
    d.mkdir(parents=True, exist_ok=True)
    (d / "acceptance.md").write_text(
        "# acceptance\n- R-009.1.1: renders headings\n- R-009.1.2: escapes html\n",
        encoding="utf-8",
    )
    return d


def _no_tests_metadata(child_address=LEAF, *, parent=PARENT):
    parent_dir = addressing.node_dir(parent, ledger.RUNTIME_ROOT)
    rel = f"exceptions/no-executable-tests-{addressing.node_path(child_address).replace('/', '-')}.json"
    path = parent_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "type": "no_executable_tests_exception",
            "target": child_address,
            "approved_by": parent,
            "reason": "test fixture exercises node form preinstantiation without executable tests",
        }),
        encoding="utf-8",
    )
    return {"no_executable_tests_exception_ref": rel}


def _drive(level="L5", brief="do the task serving R-009.1.1 and R-009.1.2"):
    chokepoint.set_adapter(FakeAdapter())
    child_metadata = _no_tests_metadata(LEAF) if level == "L5" else None
    return chokepoint.register_and_spawn_child(
        PARENT, LEAF,
        child_level_config=config.LevelConfig.for_level(level),
        brief_content=brief,
        child_metadata=child_metadata,
    )


def test_spawn_pre_instantiates_report_and_plan_forms(runtime):
    """The fresh child node receives report.md + plan.md skeletons: header pre-filled
    (address, parent), the GIVEN IDs pre-listed in the Requirement-IDs section, the
    template prompts' identity placeholders resolved, the unfilled sentinel present."""
    _seed_parent()
    child_dir = _prepare_child_acceptance()

    result = _drive()
    assert getattr(result, "ok", False) is True, f"spawn must succeed: {result!r}"

    report = (child_dir / "report.md").read_text(encoding="utf-8")
    assert LEAF in report and PARENT in report, "the From/To header is pre-filled"
    assert "<node-address>" not in report and "<parent-address>" not in report
    assert "R-009.1.1" in report and "R-009.1.2" in report, (
        "the GIVEN requirement IDs are pre-listed — zero transcription ambiguity"
    )
    assert "form:unfilled" in report, (
        "the skeleton must carry the unfilled sentinel — otherwise it auto-defeats E2"
    )
    for heading in (
        "## Drove and Watched",
        "## Inferred",
        "## Residual Uncertainty",
        "## Inventory",
    ):
        assert heading in report, f"L5 report form is missing {heading}"

    plan = (child_dir / "plan.md").read_text(encoding="utf-8")
    assert LEAF in plan, "the plan skeleton names the node"
    assert "report.md" in plan and "Sign off" in plan, "the standing final-three items survive"


def test_forms_do_not_overwrite_existing_first_spawn_forms(runtime):
    """Existing node-root forms are respected when there is no terminal prior incarnation.

    Parent-authored or fixture-authored forms in a prepared-but-never-terminal node are not clobbered
    by the skeleton helper. Terminal same-address reincarnation freshening is covered in outbox tests.
    """
    _seed_parent()
    child_dir = _prepare_child_acceptance()
    (child_dir / "report.md").write_text("PRIOR-REPORT-CONTENT\n", encoding="utf-8")
    (child_dir / "plan.md").write_text("PRIOR-PLAN-CONTENT\n", encoding="utf-8")

    result = _drive()
    assert getattr(result, "ok", False) is True
    assert (child_dir / "report.md").read_text(encoding="utf-8") == "PRIOR-REPORT-CONTENT\n"
    assert (child_dir / "plan.md").read_text(encoding="utf-8") == "PRIOR-PLAN-CONTENT\n"


def test_l5_plus_seat_gets_the_verified_variant(runtime):
    """The reviewer's form says VERIFIED, never discharged (the registry's named adaptation)."""
    _seed_parent()
    child_dir = _prepare_child_acceptance()

    result = _drive(level="L5+")
    assert getattr(result, "ok", False) is True
    report = (child_dir / "report.md").read_text(encoding="utf-8")
    assert "verified" in report.lower(), "the L5+ seat gets the verified-variant template"


@pytest.mark.parametrize(
    ("level", "artifact"),
    [
        ("L4+", "gate-composition-report.md"),
        ("L3+", "gate-area-composition-review.md"),
        ("L2+", "gate-composition-review.md"),
    ],
)
def test_higher_plus_review_seats_require_their_gate_artifact(runtime, level, artifact):
    """A plus-level review binding must be checked against the portfolio gate artifact.

    Mutant killed: artifact lookup keyed only on bare L4/L3/L2 would let an L4+/L3+/L2+ review
    seat sign DONE with only report.md, bypassing the altitude-specific verdict artifact.
    """
    from harnessd import return_contract

    review_addr = "proj/widget#review"
    node_dir = addressing.node_dir(review_addr, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "report.md").write_text("# review report\n", encoding="utf-8")
    binding = {
        "node_address": review_addr,
        "level": level,
        "gate_for": "proj/widget#exec",
    }

    assert return_contract.gate_artifact_name(binding) == artifact
    verdict = return_contract.check_done_contract(review_addr, binding)
    assert verdict.ok is False
    assert any("MISSING-GATE-ARTIFACT" in d and artifact in d for d in verdict.defects)

    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / artifact).write_text("Verdict: ACCEPT\n\nLooks coherent.\n", encoding="utf-8")
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\n**Review Mode:** SHORT\n**Short Review Exception:** YES\n\n"
        "## Role Selection\n\n"
        "Short review selected because every exception condition has direct evidence.\n\n"
        "## Short Review Exception\n\n"
        "| Condition | YES | Evidence Pointer | Rationale |\n"
        "|---|---|---|---|\n"
        "| Output count is 1-2 | YES | report.md | one submitted output |\n"
        "| No shared dependency is named | YES | report.md | no dependency named |\n"
        "| Obligations map to one output | YES | report.md | mapping is direct |\n"
        "| Local review has verdict and evidence | YES | report.md | verdict pointer present |\n"
        "| Boundary is unchanged or evidenced | YES | report.md | no boundary change named |\n"
        "| Handoff names evidence and risks | YES | report.md | handoff is explicit |\n",
        encoding="utf-8",
    )
    verdict2 = return_contract.check_done_contract(review_addr, binding)
    assert verdict2.ok is True, f"filled {artifact} should satisfy the gate floor: {verdict2.defects!r}"


@pytest.mark.parametrize(
    ("level", "artifact"),
    [
        ("L4+", "gate-composition-report.md"),
        ("L3+", "gate-area-composition-review.md"),
        ("L2+", "gate-composition-review.md"),
    ],
)
def test_higher_plus_review_artifact_satisfies_without_generic_report(runtime, level, artifact):
    """Higher review seats use the named gate artifact instead of a second review report.md."""
    from harnessd import return_contract

    review_addr = "proj/widget#review"
    node_dir = addressing.node_dir(review_addr, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / artifact).write_text("VERDICT: ACCEPT\n\nPackage is coherent.\n", encoding="utf-8")
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\nReview Mode: SHORT\nShort Review Exception: YES\n\n"
        "## Role Selection\n\nShort review selected because every exception condition has evidence.\n\n"
        "## Short Review Exception\n\n"
        "| Condition | YES | Evidence Pointer | Rationale |\n"
        "|---|---|---|---|\n"
        "| Output count is 1-2 | YES | composition artifact | one submitted output |\n"
        "| No shared dependency is named | YES | composition artifact | no dependency named |\n"
        "| Obligations map to one output | YES | composition artifact | mapping is direct |\n"
        "| Local review has verdict and evidence | YES | composition artifact | verdict pointer present |\n"
        "| Boundary is unchanged or evidenced | YES | composition artifact | no boundary change named |\n"
        "| Handoff names evidence and risks | YES | composition artifact | handoff is explicit |\n",
        encoding="utf-8",
    )
    binding = {
        "node_address": review_addr,
        "level": level,
        "gate_for": "proj/widget#exec",
    }

    verdict = return_contract.check_done_contract(review_addr, binding)

    assert verdict.ok is True, verdict.defects
    assert not (node_dir / "report.md").exists()


def _write_short_higher_gate_artifacts(review_addr: str, artifact: str, *, verdict: str = "ACCEPT"):
    node_dir = addressing.node_dir(review_addr, ledger.RUNTIME_ROOT)
    node_dir.mkdir(parents=True, exist_ok=True)
    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / artifact).write_text(f"VERDICT: {verdict}\n\nPackage is coherent.\n", encoding="utf-8")
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\n"
        "Review Mode: SHORT\n"
        "Short Review Exception: YES\n\n"
        "## Role Selection\n\n"
        "Short review selected because every exception condition has direct evidence.\n\n"
        "## Short Review Exception\n\n"
        "| Condition | YES | Evidence Pointer | Rationale |\n"
        "|---|---|---|---|\n"
        "| Output count is 1-2 | YES | gate artifact | one submitted output |\n"
        "| No shared dependency is named | YES | gate artifact | no dependency named |\n"
        "| Obligations map to one output | YES | gate artifact | mapping is direct |\n"
        "| Local review has verdict and evidence | YES | gate artifact | verdict pointer present |\n"
        "| Boundary is unchanged or evidenced | YES | gate artifact | no boundary change named |\n"
        "| Handoff names evidence and risks | YES | gate artifact | handoff is explicit |\n",
        encoding="utf-8",
    )


def test_l3_plus_accept_requires_l4_child_gate_pass_evidence(runtime):
    """An area gate cannot accept an L3 that built inline instead of driving L4 children."""
    from harnessd import return_contract

    producer_addr = "proj/parser#exec"
    review_addr = "proj/parser#review"
    producer = {
        "node_address": producer_addr,
        "parent_address": "proj#exec",
        "level": "L3",
        "state": "done",
        "gate_state": "candidate_submitted",
    }
    review = {
        "node_address": review_addr,
        "level": "L3+",
        "gate_for": producer_addr,
    }
    ledger.write_binding({producer_addr: producer, review_addr: review}, _lock_held=True)
    _write_short_higher_gate_artifacts(review_addr, "gate-area-composition-review.md")

    verdict = return_contract.check_done_contract(review_addr, review)

    assert verdict.ok is False
    assert any("MISSING-LOWER-EXECUTION-EVIDENCE" in d for d in verdict.defects)

    child = {
        "node_address": "proj/parser/ingest#exec",
        "parent_address": producer_addr,
        "level": "L4",
        "state": "done",
        "gate_state": "gate_passed",
    }
    ledger.write_binding(
        {**ledger.all_nodes(), child["node_address"]: child},
        _lock_held=True,
    )

    verdict2 = return_contract.check_done_contract(review_addr, review)
    assert verdict2.ok is True, verdict2.defects


def test_planning_l3_review_accept_exempts_lower_execution_evidence(runtime):
    """A design-only planning L3 can be accepted without L4 execution evidence when marked as such."""
    from harnessd import return_contract

    producer_addr = "proj/parser-plan#exec"
    review_addr = "proj/parser-plan#review"
    producer = {
        "node_address": producer_addr,
        "parent_address": "proj#exec",
        "level": "L3",
        "child_purpose": "planning",
        "state": "done",
        "gate_state": "candidate_submitted",
    }
    review = {
        "node_address": review_addr,
        "level": "L3+",
        "gate_for": producer_addr,
    }
    ledger.write_binding({producer_addr: producer, review_addr: review}, _lock_held=True)
    _write_short_higher_gate_artifacts(review_addr, "gate-area-composition-review.md")

    verdict = return_contract.check_done_contract(review_addr, review)

    assert verdict.ok is True, verdict.defects


def test_l4_plus_accept_ignores_test_author_when_requiring_implementation_l5(runtime):
    """Test-author children support acceptance refresh, but do not prove implementation execution."""
    from harnessd import return_contract

    producer_addr = "proj/parser/ingest#exec"
    review_addr = "proj/parser/ingest#review"
    producer = {
        "node_address": producer_addr,
        "parent_address": "proj/parser#exec",
        "level": "L4",
        "state": "done",
        "gate_state": "candidate_submitted",
    }
    review = {
        "node_address": review_addr,
        "level": "L4+",
        "gate_for": producer_addr,
    }
    test_author = {
        "node_address": "proj/parser/ingest/acceptance-refresh#exec",
        "parent_address": producer_addr,
        "level": "L5",
        "child_purpose": "test_author",
        "state": "done",
        "gate_state": "gate_passed",
    }
    ledger.write_binding(
        {
            producer_addr: producer,
            review_addr: review,
            test_author["node_address"]: test_author,
        },
        _lock_held=True,
    )
    _write_short_higher_gate_artifacts(review_addr, "gate-composition-report.md")

    verdict = return_contract.check_done_contract(review_addr, review)

    assert verdict.ok is False
    assert any("MISSING-LOWER-EXECUTION-EVIDENCE" in d for d in verdict.defects)


@pytest.mark.parametrize(
    ("level", "artifact"),
    [
        ("L4+", "gate-composition-report.md"),
        ("L3+", "gate-area-composition-review.md"),
        ("L2+", "gate-composition-review.md"),
    ],
)
def test_higher_plus_review_gate_artifact_requires_verdict(runtime, level, artifact):
    """A present portfolio gate artifact is still invalid without an explicit routing verdict."""
    from harnessd import return_contract

    review_addr = "proj/widget#review"
    node_dir = addressing.node_dir(review_addr, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "report.md").write_text("# review report\n", encoding="utf-8")
    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / artifact).write_text(
        "# gate artifact\n\nThe submitted work looks coherent, but no route is declared.\n",
        encoding="utf-8",
    )
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\n**Review Mode:** SHORT\n**Short Review Exception:** YES\n\n"
        "## Role Selection\n\n"
        "Short review selected because every exception condition has direct evidence.\n\n"
        "## Short Review Exception\n\n"
        "| Condition | YES Evidence Pointer | Rationale |\n"
        "|---|---|---|\n"
        "| Output count is 1-2 | report.md | one submitted output |\n"
        "| No shared dependency is named | report.md | no dependency named |\n"
        "| Obligations map to one output | report.md | mapping is direct |\n"
        "| Local review has verdict and evidence | report.md | verdict pointer present |\n"
        "| Boundary is unchanged or evidenced | report.md | no boundary change named |\n"
        "| Handoff names evidence and risks | report.md | handoff is explicit |\n",
        encoding="utf-8",
    )
    binding = {
        "node_address": review_addr,
        "level": level,
        "gate_for": "proj/widget#exec",
    }

    verdict = return_contract.check_done_contract(review_addr, binding)
    assert verdict.ok is False
    assert any(
        "MISSING-GATE-VERDICT" in d
        and artifact in d
        and "ACCEPT" in d
        and "BOUNCE" in d
        and "ESCALATE" in d
        for d in verdict.defects
    )


def test_higher_review_plan_must_record_role_selection(runtime):
    """A review plan that names FULL mode but omits role selection is not a real dispatch plan."""
    from harnessd import return_contract

    review_addr = "proj/widget#review"
    node_dir = addressing.node_dir(review_addr, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "report.md").write_text("# review report\n", encoding="utf-8")
    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / "gate-composition-report.md").write_text("Verdict: ACCEPT\n", encoding="utf-8")
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text("# plan\n\n**Review Mode:** FULL\n", encoding="utf-8")
    _write_canonical_full_review_reports(gate_dir)
    binding = {
        "node_address": review_addr,
        "level": "L4+",
        "gate_for": "proj/widget#exec",
    }

    verdict = return_contract.check_done_contract(review_addr, binding)
    assert verdict.ok is False
    assert any("MISSING-ROLE-SELECTION" in d for d in verdict.defects)


def test_higher_review_reports_need_plain_routing_line(runtime):
    """A routing heading is readable to humans but not the v1 machine-checked report line."""
    from harnessd import return_contract

    review_addr = "proj/widget#review"
    node_dir = addressing.node_dir(review_addr, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "report.md").write_text("# review report\n", encoding="utf-8")
    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / "gate-composition-report.md").write_text("Verdict: ACCEPT\n", encoding="utf-8")
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\n"
        "Review Mode: FULL\n\n"
        "## Role Selection\n\n"
        "Selected checks: fidelity-coverage, composition-interface, evidence-credibility, "
        "risk-readiness. This set covers coverage, interfaces, integration, evidence, "
        "boundary quality, and parent consumability.\n",
        encoding="utf-8",
    )
    for name in review_dispatch.required_check_report_names("L4+"):
        path = gate_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {name}\n\n## Recommended Routing: accept-note\n\nNo material finding.\n",
            encoding="utf-8",
        )
    binding = {
        "node_address": review_addr,
        "level": "L4+",
        "gate_for": "proj/widget#exec",
    }

    verdict = return_contract.check_done_contract(review_addr, binding)
    assert verdict.ok is False
    assert any(
        "MISSING-REVIEWER-ROUTING" in d and "plain line" in d
        for d in verdict.defects
    )


def test_full_higher_review_requires_completed_review_check_seat_provenance(runtime):
    """FULL-mode report files are not enough; they must come from current check seats."""
    from harnessd import return_contract

    producer_addr = "proj/widget#exec"
    review_addr = "proj/widget#review"
    node_dir = addressing.node_dir(review_addr, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\n"
        "Review Mode: FULL\n\n"
        "## Role Selection\n\n"
        "Selected all four V1 review-check axes for a normal upper gate: "
        "fidelity-coverage, composition-interface, evidence-credibility, risk-readiness.\n",
        encoding="utf-8",
    )
    _write_canonical_full_review_reports(gate_dir)
    (gate_dir / "gate-composition-report.md").write_text(
        "# composition report\n\nVERDICT: ACCEPT — workstream composes.\n",
        encoding="utf-8",
    )
    ledger.write_binding(
        {
            producer_addr: {
                "node_address": producer_addr,
                "level": "L4",
                "state": "running",
                "gate_state": "candidate_submitted",
                "gate_id": "gate-test",
                "gate_review_dir": str(gate_dir),
                "gate_review_packet": str(gate_dir / "review-packet.md"),
            },
            review_addr: {
                "node_address": review_addr,
                "level": "L4+",
                "state": "running",
                "gate_for": producer_addr,
            },
        },
        _lock_held=True,
    )

    verdict = return_contract.check_done_contract(
        review_addr,
        {
            "node_address": review_addr,
            "level": "L4+",
            "gate_for": producer_addr,
        },
    )

    assert verdict.ok is False
    assert any("MISSING-REVIEW-CHECK-SEAT" in d for d in verdict.defects)


def test_higher_review_short_mode_requires_exception_evidence_rows(runtime):
    """SHORT mode requires more than a marker line; every exception condition needs evidence."""
    from harnessd import return_contract

    review_addr = "proj/widget#review"
    node_dir = addressing.node_dir(review_addr, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "report.md").write_text("# review report\n", encoding="utf-8")
    gate_dir = node_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / "gate-composition-report.md").write_text("Verdict: ACCEPT\n", encoding="utf-8")
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\n"
        "**Review Mode:** SHORT\n"
        "**Short Review Exception:** YES\n\n"
        "## Role Selection\n\n"
        "Short review selected for a simple candidate.\n",
        encoding="utf-8",
    )
    binding = {
        "node_address": review_addr,
        "level": "L4+",
        "gate_for": "proj/widget#exec",
    }

    verdict = return_contract.check_done_contract(review_addr, binding)
    assert verdict.ok is False
    assert any("INCOMPLETE-SHORT-REVIEW-EVIDENCE" in d for d in verdict.defects)


def test_e2_refuses_a_done_on_an_unfilled_form(runtime):
    """THE GATE STAYS HONEST: a DONE whose report.md still carries the form:unfilled sentinel
    is refused (UNFILLED-REPORT-FORM) — the pre-instantiated skeleton must never auto-satisfy
    MISSING-REPORT or the citation check. (Mutant: sentinel unchecked -> skeleton sails
    through E2 -> caught.)"""
    from harnessd import return_contract

    _seed_parent()
    child_dir = _prepare_child_acceptance()
    result = _drive()
    assert getattr(result, "ok", False) is True

    binding = ledger.read_binding(LEAF)
    verdict = return_contract.check_done_contract(LEAF, binding)
    assert verdict.ok is False
    assert any("UNFILLED-REPORT-FORM" in d for d in verdict.defects), (
        f"an untouched skeleton must be refused as UNFILLED-REPORT-FORM; got {verdict.defects!r}"
    )

    # the agent fills the form (sentinel removed, IDs kept as citations) -> the walker passes
    report = (child_dir / "report.md").read_text(encoding="utf-8")
    filled = "\n".join(
        line for line in report.splitlines() if "form:unfilled" not in line
    ).replace("<", "").replace(">", "")
    (child_dir / "report.md").write_text(
        filled + "\nDid the work.\nThe literal `form:unfilled` marker was removed.\n",
        encoding="utf-8",
    )
    verdict2 = return_contract.check_done_contract(LEAF, binding)
    assert verdict2.ok is True, f"a filled form must pass; got {verdict2.defects!r}"
