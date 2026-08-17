import json
from pathlib import Path
from types import SimpleNamespace

from harnessd import addressing, ledger, review_dispatch


def test_dispatch_section_headings_accept_descriptive_suffixes():
    for heading in (
        "## Role Selection — four V1 review axes",
        "## Role Selection – four V1 review axes",
        "## Role Selection - four V1 review axes",
        "## Role Selection: four V1 review axes",
        "## Role Selection (four V1 review axes)",
    ):
        text = f"# Review Plan\n\n{heading}\n\nfidelity-coverage\n\n## Other\n\nx\n"
        assert "fidelity-coverage" in review_dispatch._section_text(text, "Role Selection")

    bad = "# Review Plan\n\n## Role Selection Details\n\nfidelity-coverage\n"
    assert review_dispatch._section_text(bad, "Role Selection") == ""


def test_short_exception_heading_accepts_descriptive_suffix():
    text = (
        "# Review Plan\n\n"
        "## Short Review Exception — evidence rows\n\n"
        "| Condition | YES | Evidence Pointer | Rationale |\n"
        "|---|---|---|---|\n"
        "| tiny candidate | YES | review-packet.md | one bounded review is enough |\n"
    )

    assert "tiny candidate" in review_dispatch._section_text(text, "Short Review Exception")


def test_verification_runtime_pointers_include_explicit_commands_and_local_probe(
    tmp_path,
    monkeypatch,
):
    node_dir = tmp_path / "node"
    tests_dir = node_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_acceptance.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    (node_dir / "acceptance.md").write_text(
        "## Verification Commands\n\n"
        "```bash\n"
        "$ python3 -m pytest tests\n"
        "python3 -m unittest discover -s tests\n"
        "```\n",
        encoding="utf-8",
    )
    (node_dir / "brief.md").write_text(
        "## Artifact-Declared Verification Commands\n\n"
        "- `npm test`\n",
        encoding="utf-8",
    )

    def fake_which(name):
        if name in {"python3.11", "python3"}:
            return f"/fake/{name}"
        return None

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0 if argv[0] == "/fake/python3.11" else 1)

    monkeypatch.setattr(review_dispatch.shutil, "which", fake_which)
    monkeypatch.setattr(review_dispatch.subprocess, "run", fake_run)

    lines = review_dispatch._verification_runtime_pointers(node_dir)

    assert "- Explicit artifact-declared verification command(s):" in lines
    assert "  - `python3 -m pytest tests`" in lines
    assert "  - `python3 -m unittest discover -s tests`" in lines
    assert "  - `npm test`" in lines
    assert "- Pytest-capable Python: `python3.11 -m pytest`" in lines
    assert "- Stdlib unittest fallback: `python3.11 -m unittest discover -s tests`" in lines


def test_verification_runtime_pointers_ignore_command_looking_prose(
    tmp_path,
    monkeypatch,
):
    node_dir = tmp_path / "node"
    node_dir.mkdir(parents=True)
    (node_dir / "acceptance.md").write_text(
        "Run the frozen suite with `python3 -m pytest tests`.\n"
        "The reviewer may also mention `npm test` as an example.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(review_dispatch.shutil, "which", lambda _name: None)

    lines = review_dispatch._verification_runtime_pointers(node_dir)

    assert "- Explicit artifact-declared verification command(s): none found" in lines
    assert any("not from prose examples" in line for line in lines)
    assert "  - `python3 -m pytest tests`" not in lines
    assert "  - `npm test`" not in lines


def test_verification_runtime_pointers_parse_report_commands(tmp_path, monkeypatch):
    node_dir = tmp_path / "node"
    node_dir.mkdir(parents=True)
    (node_dir / "report.md").write_text(
        "## Verification evidence\n\n"
        "Collected 14 tests; red before implementation.\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "python3 -m pytest tests/ -q\n"
        "```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(review_dispatch.shutil, "which", lambda _name: None)

    lines = review_dispatch._verification_runtime_pointers(node_dir)

    assert "- Explicit artifact-declared verification command(s):" in lines
    assert "  - `python3 -m pytest tests/ -q`" in lines


def test_verification_runtime_pointers_parse_bare_command_fence(tmp_path, monkeypatch):
    node_dir = tmp_path / "node"
    node_dir.mkdir(parents=True)
    (node_dir / "report.md").write_text(
        "## Verification Commands\n\n"
        "```\n"
        "grep -o 'id: [A-Za-z0-9.-]*' design.md | sort | uniq -d\n"
        "```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(review_dispatch.shutil, "which", lambda _name: None)

    lines = review_dispatch._verification_runtime_pointers(node_dir)

    assert "- Explicit artifact-declared verification command(s):" in lines
    assert (
        "  - `grep -o 'id: [A-Za-z0-9.-]*' design.md | sort | uniq -d`"
        in lines
    )


def test_verification_runtime_pointers_ignore_non_shell_result_fences(
    tmp_path,
    monkeypatch,
):
    node_dir = tmp_path / "node"
    node_dir.mkdir(parents=True)
    (node_dir / "report.md").write_text(
        "## Verification Commands\n\n"
        "```sh\n"
        "python3 -m py_compile linecheck.py\n"
        "python3 -m pytest tests -q\n"
        "```\n\n"
        "Expected acceptance result shape:\n\n"
        "```text\n"
        "10 passed\n"
        "```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(review_dispatch.shutil, "which", lambda _name: None)

    lines = review_dispatch._verification_runtime_pointers(node_dir)

    assert "- Explicit artifact-declared verification command(s):" in lines
    assert "  - `python3 -m py_compile linecheck.py`" in lines
    assert "  - `python3 -m pytest tests -q`" in lines
    assert "  - `10 passed`" not in lines


def test_review_packet_names_expected_lower_execution_evidence(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        producer_address = "proj/parser#exec"
        review_address = "proj/parser#review"
        child_address = "proj/parser/ingest#exec"
        producer = {
            "node_address": producer_address,
            "parent_address": "proj#exec",
            "level": "L3",
            "state": "done",
        }
        child = {
            "node_address": child_address,
            "parent_address": producer_address,
            "level": "L4",
            "state": "done",
            "gate_state": "gate_passed",
        }
        node_dir = addressing.node_dir(producer_address, tmp_path)
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "report.md").write_text("# report\n", encoding="utf-8")
        (node_dir / "plan.md").write_text("# plan\n\n- [ ] sign off\n", encoding="utf-8")
        (node_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        ledger.write_binding(
            {producer_address: producer, child_address: child},
            _lock_held=True,
        )

        fields = review_dispatch.create_review_packet(
            producer_address,
            producer,
            review_address,
            "signal-1",
        )

        text = Path(fields["gate_review_packet"]).read_text(encoding="utf-8")
        manifest = Path(fields["gate_candidate_artifact_manifest"])
        manifest_text = json.loads(manifest.read_text(encoding="utf-8"))
        artifact_entries = {entry["path"]: entry for entry in manifest_text["artifacts"]}
        artifact_paths = set(artifact_entries)
        snapshot_dir = Path(fields["gate_candidate_artifact_snapshot_dir"])
        assert fields["gate_candidate_artifact_manifest_sha256"]
        assert manifest_text["schema_version"] == 2
        assert manifest_text["snapshot_root"] == str(snapshot_dir)
        assert "## Candidate Artifact Manifest" in text
        assert "Mutable process bookkeeping" in text
        assert "Snapshot copies under the review directory preserve the exact submitted bytes" in text
        assert str(manifest) in text
        assert str(snapshot_dir) in text
        assert "report.md" in artifact_paths
        assert "app.py" in artifact_paths
        assert "plan.md" not in artifact_paths
        assert "reviews" not in {p.split("/", 1)[0] for p in artifact_paths}
        assert artifact_entries["app.py"]["source_path"] == str(node_dir / "app.py")
        assert artifact_entries["app.py"]["snapshot_relpath"] == "candidate-snapshot/app.py"
        assert Path(artifact_entries["app.py"]["snapshot_path"]).read_text(encoding="utf-8") == "VALUE = 1\n"
        (node_dir / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        assert Path(artifact_entries["app.py"]["snapshot_path"]).read_text(encoding="utf-8") == "VALUE = 1\n"
        assert "## Expected Lower Execution Evidence" in text
        assert "direct `L4` execution child" in text
        assert f"`{child_address}`" in text
        assert "gate_state `gate_passed`" in text
        assert "requires these exact report files" in text
        assert "matching current-gate child-completion inbox rows before synthesis" in text
        assert "report files alone are not completion evidence" in text
        assert "`reviewers/fidelity-coverage/report.md`" in text
        assert "`reviewers/composition-interface/report.md`" in text
        assert "`reviewers/evidence-credibility/report.md`" in text
        assert "`reviewers/risk-readiness/report.md`" in text
        assert "`area-coverage`" not in text
        assert "`internal-interface-fit`" not in text
        assert "`area-integration`" not in text
        assert "`exposed-contract`" not in text
        assert "`evidence-quality`" not in text
        assert "`risk-and-deviation`" not in text
        assert "plain literal verdict line" in text
        assert "VERDICT: ACCEPT" in text
        assert "terminal-signal evidence alone is not enough" in text
        assert "Parent-visible completion happens only after" in text
        assert "routes `gate_passed`" in text
    finally:
        ledger.RUNTIME_ROOT = previous


def test_candidate_artifact_manifest_defects_detect_snapshot_drift(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        producer_address = "proj/parser#exec"
        review_address = "proj/parser#review"
        producer = {
            "node_address": producer_address,
            "parent_address": "proj#exec",
            "level": "L5",
            "state": "running",
        }
        node_dir = addressing.node_dir(producer_address, tmp_path)
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "report.md").write_text("# report\n", encoding="utf-8")
        (node_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        ledger.write_binding({producer_address: producer}, _lock_held=True)

        fields = review_dispatch.create_review_packet(
            producer_address,
            producer,
            review_address,
            "signal-1",
        )
        live = ledger.read_binding(producer_address)
        live.update(fields)
        ledger.write_binding({producer_address: live}, _lock_held=True)

        manifest = json.loads(Path(fields["gate_candidate_artifact_manifest"]).read_text(encoding="utf-8"))
        app_entry = next(entry for entry in manifest["artifacts"] if entry["path"] == "app.py")
        Path(app_entry["snapshot_path"]).write_text("VALUE = 999\n", encoding="utf-8")

        defects = review_dispatch.candidate_artifact_manifest_defects_for_review(
            {"gate_for": producer_address}
        )

        assert any("CANDIDATE-ARTIFACT-SNAPSHOT-DRIFT" in defect for defect in defects)
    finally:
        ledger.RUNTIME_ROOT = previous


def test_review_packet_names_executable_acceptance_package(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        producer_address = "proj/parser/impl#exec"
        review_address = "proj/parser/impl#review"
        producer = {
            "node_address": producer_address,
            "parent_address": "proj/parser#exec",
            "level": "L5",
            "state": "running",
        }
        node_dir = addressing.node_dir(producer_address, tmp_path)
        tests_dir = node_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "brief.md").write_text("# brief\n\nBuild against the accepted tests.\n", encoding="utf-8")
        (node_dir / "report.md").write_text("# report\n", encoding="utf-8")
        (tests_dir / "test_acceptance.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
        ledger.write_binding({producer_address: producer}, _lock_held=True)

        fields = review_dispatch.create_review_packet(
            producer_address,
            producer,
            review_address,
            "signal-1",
        )

        text = Path(fields["gate_review_packet"]).read_text(encoding="utf-8")
        assert "Frozen executable acceptance package" in text
        assert str(tests_dir) in text
        assert "no node-local acceptance/rubric file found" not in text
    finally:
        ledger.RUNTIME_ROOT = previous


def test_product_review_packet_points_to_existing_intent_rubric_not_missing_acceptance(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        producer_address = "proj/linecheck#exec"
        review_address = "proj/linecheck#review"
        child_address = "proj/linecheck/core#exec"
        producer = {
            "node_address": producer_address,
            "parent_address": "proj#exec",
            "level": "L2",
            "state": "done",
        }
        review = {
            "node_address": review_address,
            "parent_address": "proj#exec",
            "level": "L2+",
            "gate_for": producer_address,
            "state": "planned",
        }
        child = {
            "node_address": child_address,
            "parent_address": producer_address,
            "level": "L3",
            "state": "done",
            "gate_state": "gate_passed",
        }
        node_dir = addressing.node_dir(producer_address, tmp_path)
        (node_dir / "client-brief").mkdir(parents=True, exist_ok=True)
        (node_dir / "plan").mkdir(parents=True, exist_ok=True)
        (node_dir / "plan-alignment").mkdir(parents=True, exist_ok=True)
        (node_dir / "brief.md").write_text("# project brief\n", encoding="utf-8")
        (node_dir / "report.md").write_text("# report\n", encoding="utf-8")
        (node_dir / "client-brief" / "intent-spec.md").write_text(
            "# intent\nR-001\n",
            encoding="utf-8",
        )
        (node_dir / "plan" / "validated-plan-package.md").write_text(
            "# plan package\n",
            encoding="utf-8",
        )
        alignment = node_dir / "plan-alignment" / "plan-alignment-decision-1.md"
        alignment.write_text("# plan alignment\nPASS\n", encoding="utf-8")
        ledger.write_binding(
            {producer_address: producer, review_address: review, child_address: child},
            _lock_held=True,
        )

        fields = review_dispatch.create_review_packet(
            producer_address,
            producer,
            review_address,
            "signal-1",
        )

        text = Path(fields["gate_review_packet"]).read_text(encoding="utf-8")
        assert "- Acceptance/rubric pointer(s):" in text
        assert f"`{node_dir / 'client-brief' / 'intent-spec.md'}`" in text
        assert f"`{node_dir / 'plan' / 'validated-plan-package.md'}`" in text
        assert f"`{alignment}`" in text
        assert f"`{node_dir / 'acceptance.md'}`" not in text
    finally:
        ledger.RUNTIME_ROOT = previous


def test_l5_plus_review_packet_names_local_review_not_full_dispatch(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        producer_address = "proj/parser/tokenizer#exec"
        review_address = "proj/parser/tokenizer#review"
        producer = {
            "node_address": producer_address,
            "parent_address": "proj/parser#exec",
            "level": "L5",
            "state": "running",
        }
        review = {
            "node_address": review_address,
            "parent_address": "proj/parser#exec",
            "level": "L5+",
            "gate_for": producer_address,
            "state": "planned",
        }
        node_dir = addressing.node_dir(producer_address, tmp_path)
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "brief.md").write_text("# brief\n\nR-001\n", encoding="utf-8")
        (node_dir / "acceptance.md").write_text("# acceptance\n", encoding="utf-8")
        (node_dir / "report.md").write_text("# report\n", encoding="utf-8")
        (node_dir / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
        ledger.write_binding(
            {producer_address: producer, review_address: review},
            _lock_held=True,
        )

        fields = review_dispatch.create_review_packet(
            producer_address,
            producer,
            review_address,
            "signal-1",
        )

        text = Path(fields["gate_review_packet"]).read_text(encoding="utf-8")
        assert "- Acceptance/rubric pointer(s):" in text
        assert f"`{node_dir / 'acceptance.md'}`" in text
        assert "This is a local L5+ review gate" in text
        assert "Complete the review inside this reviewer seat" in text
        assert "The daemon will not open auxiliary reviewer seats for this gate" in text
        assert "For implementation candidates, run your own testing pass" in text
        assert "Use FULL for normal" not in text
        assert "independent review-check seats" not in text
        assert "`reviewers/fidelity-coverage/report.md`" not in text
    finally:
        ledger.RUNTIME_ROOT = previous


def test_review_packet_for_planning_candidate_tolerates_bare_command_fence(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        producer_address = "proj/cli#exec"
        review_address = "proj/cli#review"
        producer = {
            "node_address": producer_address,
            "parent_address": "proj#exec",
            "level": "L3",
            "child_purpose": "planning",
            "state": "running",
        }
        review = {
            "node_address": review_address,
            "parent_address": "proj#exec",
            "level": "L3+",
            "gate_for": producer_address,
            "state": "planned",
        }
        node_dir = addressing.node_dir(producer_address, tmp_path)
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "report.md").write_text(
            "# report\n\n"
            "## Verification Commands\n\n"
            "```\n"
            "grep -o 'id: [A-Za-z0-9.-]*' design.md | sort | uniq -d\n"
            "```\n",
            encoding="utf-8",
        )
        (node_dir / "brief.md").write_text("# brief\n", encoding="utf-8")
        (node_dir / "acceptance.md").write_text("# acceptance\n", encoding="utf-8")
        (node_dir / "design.md").write_text("# design\n", encoding="utf-8")
        ledger.write_binding(
            {producer_address: producer, review_address: review},
            _lock_held=True,
        )

        fields = review_dispatch.create_review_packet(
            producer_address,
            producer,
            review_address,
            "signal-1",
        )

        text = Path(fields["gate_review_packet"]).read_text(encoding="utf-8")
        assert "child_purpose=planning" in text
        assert "not expected to carry lower execution child gate passes" in text
        assert "grep -o 'id: [A-Za-z0-9.-]*' design.md | sort | uniq -d" in text
    finally:
        ledger.RUNTIME_ROOT = previous


def test_review_check_brief_names_assigned_reviewer_report_and_no_verdict_authority(tmp_path):
    gate_dir = tmp_path / "reviews" / "gate-workstream"
    spec = review_dispatch.required_review_check_specs("L4+")[0]

    text = review_dispatch.render_review_check_brief(
        review_address="proj/area/workstream#review",
        review_binding={"level": "L4+"},
        producer_address="proj/area/workstream#exec",
        gate_id="gate-workstream",
        gate_dir=gate_dir,
        spec=spec,
    )

    assert "Review Check Brief" in text
    assert "Fidelity and Coverage" in text
    assert f"Assigned report path: `{gate_dir / 'reviewers/fidelity-coverage/report.md'}`" in text
    assert f"Write exactly one check report at `{gate_dir / 'reviewers/fidelity-coverage/report.md'}`" in text
    assert "Use the shared check report shape at `" in text
    assert "operational/shared/templates/check-review-report-template.md" in text
    assert "do not write the final `gate-*` artifact" in text
    assert "Do not change the candidate" in text
    assert "do not sign ACCEPT/BOUNCE/ESCALATE for the candidate" in text


def _seed_higher_review_plan(tmp_path, *, plan_text, report_routing="accept-note"):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    producer_address = "proj/area/workstream#exec"
    review_address = "proj/area/workstream#review"
    node_dir = addressing.node_dir(producer_address, tmp_path)
    gate_dir = node_dir / "reviews" / "gate-workstream"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(plan_text, encoding="utf-8")
    for name in review_dispatch.required_check_report_names("L4+"):
        path = gate_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {name}\n\nRecommended Routing: {report_routing}\n\nNo material finding.\n",
            encoding="utf-8",
        )
    review_binding = {
        "node_address": review_address,
        "level": "L4+",
        "gate_for": producer_address,
        "state": "running",
    }
    ledger.write_binding(
        {
            producer_address: {
                "node_address": producer_address,
                "level": "L4",
                "state": "running",
                "gate_review_dir": str(gate_dir),
            },
            review_address: review_binding,
        },
        _lock_held=True,
    )
    return previous, review_address, review_binding


def test_full_review_role_selection_must_name_four_v1_axes(tmp_path):
    previous, review_address, review_binding = _seed_higher_review_plan(
        tmp_path,
        plan_text=(
            "# Review Plan\n\n"
            "Review Mode: FULL\n\n"
            "## Role Selection\n\n"
            "Use every workstream check for this normal candidate.\n"
        ),
    )
    try:
        defects = review_dispatch.dispatch_contract_defects(review_address, review_binding)
    finally:
        ledger.RUNTIME_ROOT = previous

    assert any("INCOMPLETE-ROLE-SELECTION" in defect for defect in defects)
    assert any("fidelity-coverage" in defect for defect in defects)


def test_full_review_report_routing_must_be_allowed_enum(tmp_path):
    previous, review_address, review_binding = _seed_higher_review_plan(
        tmp_path,
        plan_text=(
            "# Review Plan\n\n"
            "Review Mode: FULL\n\n"
            "## Role Selection\n\n"
            "Use the four V1 axes: fidelity-coverage, composition-interface, "
            "evidence-credibility, risk-readiness.\n"
        ),
        report_routing="maybe later",
    )
    try:
        defects = review_dispatch.dispatch_contract_defects(review_address, review_binding)
    finally:
        ledger.RUNTIME_ROOT = previous

    assert any("MISSING-REVIEWER-ROUTING" in defect for defect in defects)


def test_full_review_report_rejects_routing_alias(tmp_path):
    previous, review_address, review_binding = _seed_higher_review_plan(
        tmp_path,
        plan_text=(
            "# Review Plan\n\n"
            "Review Mode: FULL\n\n"
            "## Role Selection\n\n"
            "Use the four V1 axes: fidelity-coverage, composition-interface, "
            "evidence-credibility, risk-readiness.\n"
        ),
    )
    gate_dir = addressing.node_dir("proj/area/workstream#exec", tmp_path) / "reviews" / "gate-workstream"
    report = gate_dir / "reviewers/fidelity-coverage/report.md"
    report.write_text(
        "# fidelity\n\nRouting: accept-note\n\nNo material finding.\n",
        encoding="utf-8",
    )
    try:
        defects = review_dispatch.dispatch_contract_defects(review_address, review_binding)
    finally:
        ledger.RUNTIME_ROOT = previous

    assert any("MISSING-REVIEWER-ROUTING" in defect for defect in defects)


def test_full_review_report_rejects_extra_routing_alias_line(tmp_path):
    previous, review_address, review_binding = _seed_higher_review_plan(
        tmp_path,
        plan_text=(
            "# Review Plan\n\n"
            "Review Mode: FULL\n\n"
            "## Role Selection\n\n"
            "Use the four V1 axes: fidelity-coverage, composition-interface, "
            "evidence-credibility, risk-readiness.\n"
        ),
    )
    gate_dir = addressing.node_dir("proj/area/workstream#exec", tmp_path) / "reviews" / "gate-workstream"
    report = gate_dir / "reviewers/fidelity-coverage/report.md"
    report.write_text(
        "# fidelity\n\n"
        "Recommended Routing: accept-note\n"
        "Routing: bounce\n\n"
        "Conflicting routing metadata.\n",
        encoding="utf-8",
    )
    try:
        defects = review_dispatch.dispatch_contract_defects(review_address, review_binding)
    finally:
        ledger.RUNTIME_ROOT = previous

    assert any("MISSING-REVIEWER-ROUTING" in defect for defect in defects)


def test_full_review_report_routing_must_be_exactly_one_line(tmp_path):
    previous, review_address, review_binding = _seed_higher_review_plan(
        tmp_path,
        plan_text=(
            "# Review Plan\n\n"
            "Review Mode: FULL\n\n"
            "## Role Selection\n\n"
            "Use the four V1 axes: fidelity-coverage, composition-interface, "
            "evidence-credibility, risk-readiness.\n"
        ),
        report_routing="accept-note\nRecommended Routing: bounce",
    )
    try:
        defects = review_dispatch.dispatch_contract_defects(review_address, review_binding)
    finally:
        ledger.RUNTIME_ROOT = previous

    assert any("MISSING-REVIEWER-ROUTING" in defect for defect in defects)


def test_short_review_exception_marker_must_be_yes(tmp_path):
    previous, review_address, review_binding = _seed_higher_review_plan(
        tmp_path,
        plan_text=(
            "# Review Plan\n\n"
            "Review Mode: SHORT\n"
            "Short Review Exception: USED\n\n"
            "## Role Selection\n\n"
            "Short review selected for a one-output candidate.\n\n"
            "## Short Review Exception\n\n"
            "| Condition | YES | Evidence Pointer | Rationale |\n"
            "|---|---|---|---|\n"
            "| Output count is 1-2 | YES | report.md | one submitted output |\n"
            "| No shared state | YES | report.md | no dependency named |\n"
            "| Obligations map to one output | YES | report.md | mapping is direct |\n"
            "| Local review has verdict | YES | report.md | verdict pointer present |\n"
            "| Boundary unchanged | YES | report.md | no boundary change named |\n"
            "| Handoff names risks | YES | report.md | handoff is explicit |\n"
        ),
    )
    try:
        defects = review_dispatch.dispatch_contract_defects(review_address, review_binding)
    finally:
        ledger.RUNTIME_ROOT = previous

    assert defects == [
        f"MISSING-SHORT-REVIEW-EXCEPTION: {review_address} used short review mode "
        "without `Short Review Exception: YES` in review-plan.md"
    ]


def test_short_review_rows_require_explicit_yes_evidence_and_rationale(tmp_path):
    previous, review_address, review_binding = _seed_higher_review_plan(
        tmp_path,
        plan_text=(
            "# Review Plan\n\n"
            "Review Mode: SHORT\n"
            "Short Review Exception: YES\n\n"
            "## Role Selection\n\n"
            "Short review selected for a one-output candidate.\n\n"
            "## Short Review Exception\n\n"
            "| Condition | Evidence Pointer | Rationale |\n"
            "|---|---|---|\n"
            "| Output count is 1-2 | report.md | one submitted output |\n"
            "| No shared state | report.md | no dependency named |\n"
            "| Obligations map to one output | report.md | mapping is direct |\n"
            "| Local review has verdict | report.md | verdict pointer present |\n"
            "| Boundary unchanged | report.md | no boundary change named |\n"
            "| Handoff names risks | report.md | handoff is explicit |\n"
        ),
    )
    try:
        defects = review_dispatch.dispatch_contract_defects(review_address, review_binding)
    finally:
        ledger.RUNTIME_ROOT = previous

    assert defects == [
        f"INCOMPLETE-SHORT-REVIEW-EVIDENCE: {review_address} short review rows must "
        "include condition, explicit YES, evidence pointer, and rationale"
    ]
