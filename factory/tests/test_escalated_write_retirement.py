"""Owner-docket Q2 — authored surfaces teach messages/open-question holds only.

Legacy ``ESCALATED`` artifacts remain readable and replayable in the harness. This file guards the
different boundary: current agent/operator instructions must not offer the old artifact as a write
choice after its owner-ruled retirement.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_operational_surfaces_have_one_read_compat_reference_and_no_authored_choice():
    occurrences: list[tuple[str, int, str]] = []
    for path in sorted((REPO_ROOT / "operational").rglob("*.md")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "ESCALATED" in line:
                occurrences.append(
                    (str(path.relative_to(REPO_ROOT)), line_number, line.strip())
                )

    assert len(occurrences) == 1, (
        "current operational surfaces may mention ESCALATED exactly once: the fenced read-side "
        f"compatibility statement in comms-protocol.md; found {occurrences!r}"
    )
    path, _line_number, line = occurrences[0]
    assert path == "operational/shared/comms-protocol.md"
    assert "read-side compatibility" in line
    assert "Do not author" in line


def test_operational_compatibility_section_is_interpretation_only():
    occurrences: list[str] = []
    for path in sorted((REPO_ROOT / "operational").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "answer-down" in text:
            occurrences.append(str(path.relative_to(REPO_ROOT)))

    assert occurrences == ["operational/shared/comms-protocol.md"]
    comms = (REPO_ROOT / occurrences[0]).read_text(encoding="utf-8")
    assert "Compatibility Inputs — Read and History Only" in comms
    assert "Do not author this shape" in comms
    assert '"type": "coordination_handoff"' not in comms
    assert "Child-authored marker" not in comms


def test_operator_skill_keeps_human_answer_normal_and_fences_answer_down_as_legacy_repair():
    skill = (REPO_ROOT / ".claude/skills/l1-l5-harness/SKILL.md").read_text(encoding="utf-8")
    flat_skill = " ".join(skill.split())

    assert "ESCALATED" not in skill
    assert "harnessctl answer <address>" in skill
    assert "REPAIR / COMPATIBILITY" in skill
    assert "legacy shim" in skill
    assert "harnessctl answer-down <child-address>" in skill
    assert "message the child" in skill
    assert "answer messages close canonical questions" in flat_skill

    normal, repair = skill.split("REPAIR / COMPATIBILITY", maxsplit=1)
    assert "harnessctl answer-down" not in normal
    assert "harnessctl answer-down" in repair


def test_living_behavior_contract_no_longer_claims_old_signal_authoring():
    communication = (REPO_ROOT / "design/COMMUNICATION.md").read_text(encoding="utf-8")
    behavioral = (REPO_ROOT / "design/BEHAVIOURAL-VALIDATION.md").read_text(encoding="utf-8")
    daemon = (REPO_ROOT / "design/DAEMON.md").read_text(encoding="utf-8")
    watchdog = (REPO_ROOT / "design/WATCHDOG.md").read_text(encoding="utf-8")
    implementation = (REPO_ROOT / "harnessd/IMPLEMENTATION-PLAN.md").read_text(encoding="utf-8")

    assert "has not yet ruled whether `.signal ESCALATED` authoring retires" not in communication
    assert "is ESCALATED to L2" not in behavioral
    assert "ESCALATED rather than papered over" not in behavioral
    assert "ESCALATED rather than papered over" not in implementation
    assert "{signal: DONE|FAILED, ts, owner_token, evidence}" in daemon
    assert "legacy read compatibility only" in daemon
    assert '{"signal": "DONE" | "FAILED"' in watchdog
    assert "reader still accepts legacy `ESCALATED` artifacts" in watchdog
