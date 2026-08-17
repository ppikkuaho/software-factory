"""Item-4 spine-v1 and frozen observatory prompt contracts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OBSERVATORY = ROOT / "system/observatory"
PROMPTS = ROOT / "system/instruments/observatory/prompts"
SPINE_V0 = OBSERVATORY / "spine.v0.md"
SPINE_V1 = OBSERVATORY / "spine.v1.md"
DERIVATION = PROMPTS / "spine-derivation.v1.md"
GRADED_CHECK = PROMPTS / "graded-check.v1.md"

SPINE_V0_SHA256 = "8a728e0788d92a41000c14d471caabe1caea9137ee185c023638f8b7441a8452"
DERIVATION_SHA256 = "2e0f60a3e2c717bc73695e8bef34ef049bbb924e26537d3888b5dd184f698ab9"
GRADED_CHECK_SHA256 = "b7b9b1ea756409e883016356894b20408de8f711b09016fd8fb56d92bf04a077"
NEUTRAL_FRAMING = (
    "the goal is to surface concrete improvement suggestions if any come up — "
    "neutral framing, never forced; absence of suggestions is a fine outcome "
    "(no quota, no reward for finding something)"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _v0_entries(text: str) -> str:
    return text.split("## Entries\n\n", 1)[1].strip()


def _v1_global_floor(text: str) -> str:
    return text.split("## Global floor\n\n", 1)[1].split(
        "\n\n## Per-seat entry grammar", 1
    )[0].strip()


def test_prompt_templates_are_frozen_and_spine_v0_is_untouched():
    assert _sha256(DERIVATION) == DERIVATION_SHA256
    assert _sha256(GRADED_CHECK) == GRADED_CHECK_SHA256
    assert _sha256(SPINE_V0) == SPINE_V0_SHA256


def test_spine_v1_carries_sp1_through_sp7_verbatim_as_binary_floor():
    v0 = SPINE_V0.read_text()
    v1 = SPINE_V1.read_text()
    assert _v1_global_floor(v1) == _v0_entries(v0)
    assert "global floor applies to every seat" in v1
    assert "global-floor check is **binary**" in v1
    assert "violation is a defect" in v1


def test_spine_v1_pins_complete_p8_grammar_classes_and_ratchet():
    text = SPINE_V1.read_text()
    required = (
        "id: SP-L4-<n>",
        "statement:",
        "source citation:",
        "observable trace signal:",
        "check-cost tier: mechanical | digest | deep-read",
        "class: floor | aspiration",
        "doc-version stamp: <subject-repo git revision>",
        "Derived entries\n" "default to aspiration",
        "aspiration graduates to `floor`\n" "when it is consistently met",
    )
    for surface in required:
        assert surface in text, f"missing spine grammar surface: {surface}"


def test_l4_section_is_an_empty_scaffold_pointing_to_candidate_file():
    text = SPINE_V1.read_text()
    l4 = text.split("## L4 — Workstream Coordinator", 1)[1]
    assert "**Active set:** empty" in l4
    assert "system/observatory/spine-candidates.L4.v1.md" in l4
    assert (
        "candidate,\n" "uncitable and pending user ratification under RQ-7" in l4
    )
    assert not re.search(r"\bSP-L4-\d+\b", l4)


def test_derivation_prompt_pins_source_scope_real_anchors_and_p8_fields():
    text = DERIVATION.read_text()
    for source in (
        "operational/L4/role.md",
        "operational/L4/config.md",
        "shared protocols directly referenced by those two documents",
        "SOURCE-MANIFEST.md",
    ):
        assert source in text
    for field in (
        "id: SP-L4-<n>",
        "statement:",
        "source citation:",
        "observable trace signal:",
        "check-cost tier: mechanical | digest | deep-read",
        "class: floor | aspiration",
        "doc-version stamp: {{SUBJECT_REPO_REV}}",
    ):
        assert field in text
    assert "NO obligation may appear without a\n" "real document anchor" in text
    assert "verbatim source text" in text
    assert "Derived entries DEFAULT to `aspiration`" in text


def test_derivation_prompt_pins_budget_catalogue_coverage_and_provenance():
    text = DERIVATION.read_text()
    assert "no more than 10" in text
    assert "## ACTIVE SET" in text
    assert "## REFERENCE CATALOGUE" in text
    assert "rotates into later active sets by the lane's current focus" in text
    assert (
        "SOURCE-COVERAGE: consumed=[<every staged governing doc actually "
        "consumed>]; deliberately-excluded=[<L4-relevant surfaces outside this "
        "bounded pass, with a short reason>]"
    ) in text
    for surface in (
        "name: spine-derivation.v1.md",
        "sha256: {{PROMPT_TEMPLATE_SHA256}}",
        "subject_repo: {{SUBJECT_REPO_REV}}",
        "provenance: director-provisional-2026-07-13",
        'standing: "candidate — uncitable, pending user ratification RQ-7"',
    ):
        assert surface in text


def test_graded_check_pins_neutral_framing_grade_and_optional_note():
    text = GRADED_CHECK.read_text()
    assert text.count(NEUTRAL_FRAMING) == 1
    assert "`met-poorly`, `met`, or `met-well`" in text
    assert "improvement-opportunity note is optional" in text
    assert "Absence of a note is a complete, valid outcome" in text


def test_graded_check_pins_linkage_f7_and_m1_grammar():
    text = GRADED_CHECK.read_text()
    linkage = (
        "LINKAGE: source_expectation_id={{SOURCE_EXPECTATION_ID}}; "
        "doc_version_stamp={{SOURCE_DOC_VERSION_STAMP}}"
    )
    assert text.count(linkage) >= 2
    for surface in (
        "source_expectation_id: {{SOURCE_EXPECTATION_ID}}",
        "source_doc_version_stamp: {{SOURCE_DOC_VERSION_STAMP}}",
        "flag it, then re-validate\n" "or withdraw it",
        "kind=improvement-note",
        "harness credential",
        "user gate is TRIAGE\n" "only",
        "verifier authors it into the ledger's observatory section",
        "queued note is uncitable until adjudicated",
    ):
        assert surface in text
