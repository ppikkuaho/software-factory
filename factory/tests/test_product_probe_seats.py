from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harnessd import (
    addressing,
    config,
    fencing,
    instruction_calibration,
    ledger,
    notary,
    product_probes,
    watchdog,
)
from harnessd import review_dispatch
from harnessd.spawn import chokepoint
from harnessd.spawn.adapters.base import SpawnResult


ANCHOR_RULE = (
    "A finding may block only by citing a frozen surface — a requirement ID, an "
    "intent-spec journey, an MNF failure path, a spec'd threshold, or an explicit "
    "promise on the artifact's face; everything else is inventory, filed upward, "
    "never blocking, never bounced on."
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    return tmp_path


def _intent_text(*, include_journeys: bool = True) -> str:
    journeys = (
        "\n## Intent Journeys\n\n"
        "| Journey ID | Kind | Requirement IDs | Starting state | Action through the face | "
        "Observable result | MNF obligation | Reflect-back status |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| J-001 | positive | R-001 | empty workspace | run the documented command | "
        "a result is printed | — | confirmed |\n"
        "| J-002 | negative | R-002 | one completed request | replay the same request | "
        "the duplicate is refused | R-002 | confirmed |\n"
    ) if include_journeys else ""
    return (
        "# Intent\n\n"
        "## Requirements\n\n"
        "| ID | Requirement | Tag | MNF |\n"
        "|---|---|---|---|\n"
        "| R-001 | Produce a visible result within 250 ms. | decided | — |\n"
        "| R-002 | Never duplicate a request. | decided | YES |\n"
        f"{journeys}\n"
        "## Reflect-Back Script\n\nConfirmed.\n\n"
        "**Confirmation status:** CONFIRMED\n"
    )


def _seed_probe_inputs(
    runtime: Path,
    *,
    include_journeys: bool = True,
    include_invocation: bool = True,
) -> tuple[str, dict, Path]:
    producer = "proj#exec"
    node_dir = addressing.node_dir(producer, runtime)
    intent = node_dir / "client-brief" / "intent-spec.md"
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text(_intent_text(include_journeys=include_journeys), encoding="utf-8")
    intent_stamp = notary.stamp(intent)
    intent_receipt = notary.receipt(producer, intent, intent_stamp)

    (node_dir / "app.py").write_text("print('ready')\n", encoding="utf-8")
    report = node_dir / "report.md"
    report.write_text(
        (
            "# Product\n\n## Verification Commands\n\n```bash\npython3 app.py\n```\n"
            if include_invocation
            else "# Product\n\nThe product is assembled.\n"
        ),
        encoding="utf-8",
    )
    coverage = node_dir / "plan" / "plan-alignment-coverage.demo.json"
    coverage.parent.mkdir(parents=True, exist_ok=True)
    coverage.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pass",
                "intent_fingerprint": intent_stamp["sha256"],
                "requirements": [
                    {
                        "id": "R-001",
                        "tag": "decided",
                        "must_never_fail": False,
                        "failure_path_test_ids": [],
                    },
                    {
                        "id": "R-002",
                        "tag": "decided",
                        "must_never_fail": True,
                        "failure_path_test_ids": ["TST-R-002-FAIL"],
                    },
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    binding = {
        "node_address": producer,
        "level": "L2",
        "state": "running",
        "intent_spec_receipt": intent_receipt,
        "plan_alignment_coverage_report": str(coverage),
        "plan_alignment_coverage_report_sha256": notary.stamp(coverage)["sha256"],
    }
    ledger.write_binding({producer: binding}, _lock_held=True)
    fields = review_dispatch.create_review_packet(
        producer,
        binding,
        "proj#review",
        "signal-1",
    )
    binding.update(fields)
    ledger.write_binding({producer: binding}, _lock_held=True)
    return producer, binding, Path(fields["gate_review_dir"])


def test_level_specific_rosters_retire_evidence_credibility_only_at_l2_plus():
    l2 = [row["slug"] for row in review_dispatch.required_review_check_specs("L2+")]
    l3 = [row["slug"] for row in review_dispatch.required_review_check_specs("L3+")]
    l4 = [row["slug"] for row in review_dispatch.required_review_check_specs("L4+")]

    assert l2 == [
        "fidelity-coverage",
        "composition-interface",
        "risk-readiness",
        "user-simulation",
        "performance-robustness",
    ]
    assert l3 == [
        "fidelity-coverage",
        "composition-interface",
        "evidence-credibility",
        "risk-readiness",
    ]
    assert l4 == l3


def test_probe_charters_are_calibrated_and_carry_exact_anchor_and_threshold_rules():
    for role, path in product_probes.INSTRUCTION_PATHS.items():
        text = path.read_text(encoding="utf-8")
        assert "instruction_version: 2" in text
        assert "calibration_status: calibrated" in text
        assert "## Blocking rule" in text
        assert ANCHOR_RULE in text
        assert role in {"user-simulation", "performance-robustness"}

    performance = product_probes.INSTRUCTION_PATHS[
        "performance-robustness"
    ].read_text(encoding="utf-8")
    assert "The measurement never gates; the ANCHOR gates" in performance
    assert "Unspecified latency, throughput, resource, or scale expectations do not block" in performance
    assert "independently violates an MNF failure path or an explicit face promise" in performance


def test_l2_authored_surfaces_teach_the_five_seat_roster():
    l2_paths = (
        Path("operational/L2+/role.md"),
        Path("operational/L2+/config.md"),
        Path("operational/L2+/spawn-template.md"),
        Path("operational/shared/templates/product-composition-review-template.L2+.md"),
    )
    for path in l2_paths:
        text = path.read_text(encoding="utf-8")
        assert "reviewers/user-simulation/report.md" in text, path
        assert "reviewers/performance-robustness/report.md" in text, path
        assert "reviewers/evidence-credibility/report.md" not in text, path

    role = Path("operational/L2+/role.md").read_text(encoding="utf-8")
    assert "opens five first-class review-check seats" in role
    assert "Keep the five axes distinct" in role
    assert "### User-Simulation Product Probe" in role
    assert "### Performance And Robustness Product Probe" in role
    assert "secrets absent" in role
    assert "no accidental network exposure" in role
    assert "input sanity at trust boundaries" in role

    handbook = Path("operational/shared/review-handbook.md").read_text(
        encoding="utf-8",
    )
    assert "L4+/L3+ FULL mode uses exactly four" in handbook
    assert "L2+ FULL mode uses exactly five" in handbook
    assert "Evidence credibility retires as a separate axis at L2+" in handbook


def test_shared_templates_name_level_specific_five_and_four_rosters():
    plan = Path(
        "operational/shared/templates/higher-gate-review-plan-template.md"
    ).read_text(encoding="utf-8")
    report = Path(
        "operational/shared/templates/check-review-report-template.md"
    ).read_text(encoding="utf-8")

    assert "L2+ five-seat roster" in plan
    assert "L3+/L4+ four-seat roster" in plan
    assert "reviewers/user-simulation/report.md" in plan
    assert "reviewers/performance-robustness/report.md" in plan
    assert "reviewers/evidence-credibility/report.md" in plan
    assert "L2+ only" in report
    assert "User simulation" in report
    assert "Performance and robustness" in report


def test_intent_journey_contract_propagates_to_intake_skill_and_reference():
    contract = Path("operational/shared/intent-spec-contract.md").read_text(
        encoding="utf-8",
    )
    intake = Path("operational/L1/intake-session-template.md").read_text(
        encoding="utf-8",
    )
    skill = Path("operational/L1/skills/new-project.md").read_text(
        encoding="utf-8",
    )
    reference = Path("dry-run/intent-spec.md").read_text(encoding="utf-8")

    for text in (contract, intake, skill, reference):
        assert "Intent Journeys" in text
        assert "Journey ID" in text
        assert "Action through the face" in text
        assert "Reflect-back status" in text
    assert "reviewers never infer a journey from Outcomes prose" in contract
    assert "confirmed intent journeys" in skill


def test_probe_instructions_pass_exact_generic_notary_calibration():
    defects = product_probes.instruction_calibration_defects()
    assert defects == ()
    assert product_probes.instruction_calibration_defects(allow_uncalibrated=True) == ()

    q4 = instruction_calibration.instruction_calibration_defects(
        instruction_paths=product_probes.INSTRUCTION_PATHS.values(),
        required_channel="product_probe_calibration",
        defect_code=product_probes.UNCALIBRATED_DEFECT,
    )
    assert q4 == ()


def test_probe_roster_joins_confirmed_journeys_with_q3_mnf_paths(runtime):
    producer, binding, gate_dir = _seed_probe_inputs(runtime)

    prepared = product_probes.prepare_probe_roster(
        producer_address=producer,
        producer_binding=binding,
        gate_dir=gate_dir,
    )

    assert prepared.ok is True
    assert prepared.defects == ()
    payload = json.loads(prepared.path.read_text(encoding="utf-8"))
    assert [row["journey_id"] for row in payload["journeys"]] == ["J-001", "J-002"]
    assert payload["journeys"][1]["mnf_obligation"] == "R-002"
    assert payload["requirements"] == [
        {
            "id": "R-001",
            "must_never_fail": False,
            "tag": "decided",
            "text": "Produce a visible result within 250 ms.",
        },
        {
            "id": "R-002",
            "must_never_fail": True,
            "tag": "decided",
            "text": "Never duplicate a request.",
        },
    ]
    assert payload["mnf_failure_paths"] == [
        {
            "requirement_id": "R-002",
            "failure_path_test_ids": ["TST-R-002-FAIL"],
        }
    ]
    assert payload["invocation_commands"] == ["python3 app.py"]
    assert payload["face_findings"] == []
    assert payload["candidate_manifest_sha256"] == binding[
        "gate_candidate_artifact_manifest_sha256"
    ]
    assert oct(prepared.path.stat().st_mode & 0o777) == "0o444"
    assert prepared.sha256 == notary.stamp(prepared.path)["sha256"]


def test_missing_journey_table_refuses_without_prose_fallback(runtime):
    producer, binding, gate_dir = _seed_probe_inputs(
        runtime,
        include_journeys=False,
    )

    prepared = product_probes.prepare_probe_roster(
        producer_address=producer,
        producer_binding=binding,
        gate_dir=gate_dir,
    )

    assert prepared.ok is False
    assert product_probes.MISSING_JOURNEYS_DEFECT in prepared.defects
    assert prepared.path is None


def test_missing_declared_invocation_is_a_typed_blockable_face_finding(runtime):
    producer, binding, gate_dir = _seed_probe_inputs(
        runtime,
        include_invocation=False,
    )

    prepared = product_probes.prepare_probe_roster(
        producer_address=producer,
        producer_binding=binding,
        gate_dir=gate_dir,
    )

    assert prepared.ok is True
    payload = json.loads(prepared.path.read_text(encoding="utf-8"))
    assert payload["invocation_commands"] == []
    assert payload["face_findings"] == [
        {
            "defect": product_probes.FACE_NO_INVOCATION,
            "blocking": True,
            "anchor_kind": "artifact-face-promise",
            "anchor_pointer": binding["gate_candidate_artifact_manifest"],
        }
    ]


def test_disposable_probe_instances_are_verified_separate_writable_copies(runtime):
    producer, binding, gate_dir = _seed_probe_inputs(runtime)

    first = product_probes.prepare_disposable_instance(
        producer_address=producer,
        producer_binding=binding,
        gate_dir=gate_dir,
        probe_slug="user-simulation",
    )
    second = product_probes.prepare_disposable_instance(
        producer_address=producer,
        producer_binding=binding,
        gate_dir=gate_dir,
        probe_slug="performance-robustness",
    )

    assert first.ok is True and second.ok is True
    assert first.root != second.root
    assert (first.root / "app.py").read_text(encoding="utf-8") == "print('ready')\n"
    assert (second.root / "app.py").read_text(encoding="utf-8") == "print('ready')\n"
    os.chmod(first.root / "app.py", 0o644)
    (first.root / "app.py").write_text("print('mutated probe')\n", encoding="utf-8")
    assert (second.root / "app.py").read_text(encoding="utf-8") == "print('ready')\n"
    snapshot = Path(binding["gate_candidate_artifact_snapshot_dir"]) / "app.py"
    assert snapshot.read_text(encoding="utf-8") == "print('ready')\n"
    manifest = json.loads(first.manifest.read_text(encoding="utf-8"))
    assert manifest["candidate_manifest_sha256"] == binding[
        "gate_candidate_artifact_manifest_sha256"
    ]
    assert oct(first.manifest.stat().st_mode & 0o777) == "0o444"


def test_l2_risk_readiness_adds_exact_security_basics_only_at_product_altitude():
    l2_risk = next(
        row for row in review_dispatch.required_review_check_specs("L2+")
        if row["slug"] == "risk-readiness"
    )
    l3_risk = next(
        row for row in review_dispatch.required_review_check_specs("L3+")
        if row["slug"] == "risk-readiness"
    )

    accounting = str(l2_risk["required_accounting"])
    assert "secrets/credentials are absent from delivered artifacts and probe evidence" in accounting
    assert "no accidental network listener or exposure exists beyond the documented face" in accounting
    assert "trust-boundary inputs have basic sanity validation" in accounting
    assert "Security Basics" not in str(l3_risk["required_accounting"])


class _ProbeAdapter:
    def __init__(self):
        self.calls = []
        self.cohort_sizes = []
        self.tmux = _ProbeTmux()

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((tmux_target, level_config, neutral_brief))
        self.cohort_sizes.append(
            len(
                [
                    binding
                    for binding in ledger.all_nodes().values()
                    if binding.get("review_check_for") == "proj#review"
                ]
            )
        )
        return SpawnResult(
            ok=True,
            session_uuid=f"session-{len(self.calls)}",
            model_used="fixture / fake",
            role_variant=level_config.role_variant,
            system_prompt_file="operational/shared/system-prompt.md",
            system_prompt_file_hash="hash",
            tmux_target=f"fake-{len(self.calls)}:0.0",
            transcript_path=f"/tmp/probe-{len(self.calls)}.jsonl",
            failure_class=None,
        )


class _ProbeTmux:
    def kill(self, _target):
        return None

    def send_keys(self, _target, _text):
        return True

    def capture_pane(self, _target):
        from harnessd import watchdog

        return f"{watchdog.FORK_PROMPT}\n"


def _seed_l2_review(runtime: Path):
    producer, binding, gate_dir = _seed_probe_inputs(runtime)
    review = "proj#review"
    binding.update(
        {
            "gate_state": "candidate_submitted",
            "gate_review_address": review,
            "gate_id": gate_dir.name,
            "generation": 1,
            "lease_epoch": 1,
        }
    )
    review_binding = {
        "node_address": review,
        "parent_address": None,
        "level": "L2+",
        "role_variant": "L2+#review",
        "subagent_id": "review-sa",
        "session_uuid": "review-session",
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": fencing.mint_owner_token(
            review,
            "review-sa",
            "review-session",
            1,
        ),
        "workspace": str(addressing.node_dir(review, runtime)),
        "gate_for": producer,
    }
    ledger.write_binding(
        {producer: binding, review: review_binding},
        _lock_held=True,
    )
    (gate_dir / "review-plan.md").write_text(
        "# Review Plan\n\n"
        "Review Mode: FULL\n\n"
        "## Role Selection — five L2+ checks\n\n"
        "fidelity-coverage, composition-interface, risk-readiness, "
        "user-simulation, performance-robustness.\n",
        encoding="utf-8",
    )
    return producer, review, binding, gate_dir


def test_l2_full_dispatch_preregisters_five_and_physically_isolates_probe_inputs(
    runtime,
    monkeypatch,
):
    _producer, review, _binding, _gate_dir = _seed_l2_review(runtime)
    fake = _ProbeAdapter()
    previous = chokepoint.ADAPTER
    chokepoint.set_adapter(fake)
    try:
        results = chokepoint.dispatch_review_check_seats(
            review,
        )
    finally:
        chokepoint.set_adapter(previous)

    assert len(results) == 5
    assert all(result.ok for result in results)
    assert fake.cohort_sizes == [5, 5, 5, 5, 5]
    live = ledger.all_nodes()
    checks = {
        binding["review_check_axis"]: binding
        for binding in live.values()
        if binding.get("review_check_for") == review
    }
    assert set(checks) == {
        "fidelity-coverage",
        "composition-interface",
        "risk-readiness",
        "user-simulation",
        "performance-robustness",
    }
    assert "evidence-credibility" not in checks
    for slug in product_probes.PROBE_SLUGS:
        binding = checks[slug]
        assert binding["product_probe_role"] == slug
        assert Path(binding["product_probe_instance_root"]).is_dir()
        assert Path(binding["product_probe_instance_manifest"]).is_file()
        assert Path(binding["product_probe_roster"]).is_file()
        call = next(call for call in fake.calls if call[0] == binding["node_address"])
        level_config = call[1]
        neutral = call[2]
        assert level_config.role_variant == f"product-probe#{slug}"
        policy = neutral["containment_profile"]["read_policy"]
        assert policy["exact_declared"] is True
        assert policy["direct_surfaces"] == []
        assert str(addressing.node_dir("proj#exec", runtime)) not in policy["allow_subpaths"]


def test_probe_level_config_takes_the_production_blinders_resolution(monkeypatch):
    """Product probes carry the PRODUCTION read-policy resolution, not a hardcoded enforce.

    The identical hardcode shipped in both blind-seat level configs; probes have never dispatched
    live, so this is the never-yet-hit half of the same defect. As with the semantic cell, the
    probe's REAL read window is ``blinders.derive_exact_policy`` (unconditionally enforce — the
    isolated instance IS the instrument), so this field must simply not lie about the deployment's
    observe/enforce posture.
    """
    monkeypatch.delenv(config.BLINDERS_MODE_ENV, raising=False)
    for slug in product_probes.PROBE_SLUGS:
        assert chokepoint._product_probe_level_config(slug).blinders_mode == "observe"

    monkeypatch.setenv(config.BLINDERS_MODE_ENV, "enforce")
    for slug in product_probes.PROBE_SLUGS:
        assert chokepoint._product_probe_level_config(slug).blinders_mode == "enforce"


def test_l2_review_kickoff_names_the_five_product_altitude_reports(runtime):
    _producer, review, _binding, _gate_dir = _seed_l2_review(runtime)
    fake = _ProbeAdapter()

    chokepoint._deliver_kickoff(
        review,
        SpawnResult(
            ok=True,
            session_uuid="review-session",
            model_used="fixture / fake",
            role_variant="L2+#review",
            system_prompt_file="operational/shared/system-prompt.md",
            system_prompt_file_hash="hash",
            tmux_target="fake-review:0.0",
            transcript_path="/tmp/l2-review.jsonl",
            failure_class=None,
        ),
        fake,
    )

    rows = [
        json.loads(line)
        for line in addressing.inbox_path(review, runtime)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    message = next(row["message"] for row in rows if row["type"] == "kickoff")
    assert "naming the five L2+ product-altitude axes" in message
    assert "reviewers/user-simulation/report.md" in message
    assert "reviewers/performance-robustness/report.md" in message
    assert "reviewers/evidence-credibility/report.md" not in message


def test_l2_probe_dispatch_fails_closed_on_unstamped_instruction_before_registration(
    runtime,
    monkeypatch,
):
    _producer, review, _binding, _gate_dir = _seed_l2_review(runtime)
    unstamped_root = runtime / "unstamped-probe-instructions"
    unstamped_root.mkdir()
    unstamped = {}
    for role, source in product_probes.INSTRUCTION_PATHS.items():
        target = unstamped_root / source.name
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        unstamped[role] = target
    monkeypatch.setattr(product_probes, "INSTRUCTION_PATHS", unstamped)
    fake = _ProbeAdapter()
    previous = chokepoint.ADAPTER
    chokepoint.set_adapter(fake)
    try:
        results = chokepoint.dispatch_review_check_seats(review)
    finally:
        chokepoint.set_adapter(previous)

    assert results == []
    assert fake.calls == []
    assert not any(
        binding.get("review_check_for") == review
        for binding in ledger.all_nodes().values()
    )
    inbox = addressing.inbox_path(review, runtime)
    rows = [
        json.loads(line)
        for line in inbox.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["type"] == "review_dispatch_defect"
    assert product_probes.UNCALIBRATED_DEFECT in rows[-1]["defect"]
    assert product_probes.MODEL_UNCALIBRATED_DEFECT not in rows[-1]["defect"]
    assert config.PRODUCT_PROBE_LEVEL_CONFIGS


def test_existing_review_check_barrier_wakes_once_for_five_member_l2_cohort(runtime):
    producer = "proj#exec"
    review = "proj#review"
    producer_binding = {
        "node_address": producer,
        "parent_address": None,
        "level": "L2",
        "state": "running",
        "gate_state": "candidate_submitted",
        "gate_review_address": review,
        "gate_id": "gate-five",
    }
    review_binding = {
        "node_address": review,
        "parent_address": None,
        "level": "L2+",
        "state": "running",
        "gate_for": producer,
        "last_inbox_acked_offset": 0,
    }
    bindings = {producer: producer_binding, review: review_binding}
    addresses = []
    for slug in (
        "fidelity-coverage",
        "composition-interface",
        "risk-readiness",
        "user-simulation",
        "performance-robustness",
    ):
        address = f"proj/reviews/gate-five/reviewers/{slug}#exec"
        addresses.append(address)
        bindings[address] = {
            "node_address": address,
            "parent_address": review,
            "level": "L2+",
            "role_variant": "L2+#review-check",
            "state": "running",
            "generation": 1,
            "lease_epoch": 1,
            "review_check_for": review,
            "review_check_candidate": producer,
            "review_check_axis": slug,
            "gate_id": "gate-five",
        }
    ledger.write_binding(bindings, _lock_held=True)

    for address in addresses:
        current = ledger.all_nodes()
        current[address]["state"] = "done"
        ledger.write_binding(current, _lock_held=True)
        chokepoint._notify_parent_of_collapse(address, current[address], "DONE")

    inbox = addressing.inbox_path(review, runtime)
    rows = [
        json.loads(line)
        for line in inbox.read_text(encoding="utf-8").splitlines()
    ]
    barriers = [row for row in rows if row["type"] == "barrier_complete"]
    assert len([row for row in rows if row["type"] == "child_collapsed"]) == 5
    assert len(barriers) == 1
    assert barriers[0]["cohort"] == "review_check"
    assert len(barriers[0]["members"]) == 5
    assert watchdog.inbox_has_unacked(review_binding, review_binding) is True
