"""Repository-versioned Claude Code operator skill contract."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from harnessd import harnessctl


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / ".claude" / "skills" / "l1-l5-harness" / "SKILL.md"


def _read_skill():
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match, "Claude project skill must start with YAML frontmatter"
    return yaml.safe_load(match.group(1)), text


def _parser_choices() -> set[str]:
    choices = set()
    for action in harnessctl.build_parser()._actions:
        if getattr(action, "choices", None):
            choices.update(action.choices.keys())
    return choices


def test_skill_is_git_versioned_explicit_only_claude_project_skill():
    assert SKILL.is_file()
    frontmatter, text = _read_skill()
    assert frontmatter["name"] == "l1-l5-harness"
    assert frontmatter.get("description")
    assert frontmatter["disable-model-invocation"] is True
    assert "$ARGUMENTS" in text
    assert not (SKILL.parent / "agents" / "openai.yaml").exists()


def test_skill_documents_project_discovery_and_symlink_install_without_copy_drift():
    _frontmatter, text = _read_skill()
    assert ".claude/skills/l1-l5-harness/SKILL.md" in text
    assert "~/.claude/skills/l1-l5-harness" in text
    assert "ln -s" in text or "ln -sfn" in text
    assert "source of truth" in text.lower()


def test_skill_covers_start_status_attach_promote_and_every_current_verb():
    _frontmatter, text = _read_skill()
    for primary in ("start", "status", "attach", "promote"):
        assert re.search(rf"\b{re.escape(primary)}\b", text)
    assert "harnessctl genesis" not in text
    missing = sorted(
        command
        for command in _parser_choices()
        if not re.search(rf"(?<![\w-]){re.escape(command)}(?![\w-])", text)
    )
    assert missing == [], f"operator skill omits harnessctl verbs: {missing}"


def test_skill_keeps_harnessctl_as_the_state_boundary():
    _frontmatter, text = _read_skill()
    assert "python3 -m harnessd.harnessctl" in text
    assert "Never edit runtime state files" in text
    assert "every state action" in text.lower()
    assert "ledger.write_binding" not in text
    assert "executor.transition" not in text


def test_skill_documents_live_or_dead_observability_without_direct_state_reads():
    _frontmatter, text = _read_skill()
    assert "harnessctl view" in text
    assert "harnessctl journey" in text
    assert "--format terminal" in text
    assert "--format json" in text
    assert "--stdout" in text
    assert "dead" in text.lower() or "postmortem" in text.lower()
    assert ".harnessd/views/journey.html" in text


def test_skill_documents_owner_final_playback_and_distinct_commissioning_delegate():
    _frontmatter, text = _read_skill()
    assert "harnessctl fidelity-playback" in text
    assert "HARNESS_FIDELITY_PLAYBACK_AUTHORITY=operator-delegate" in text
    assert "--authority operator-delegate" in text
    assert "--actor" in text
    assert "OWNER-CONFIRMED-FIDELITY-PLAYBACK" in text
    assert "COMMISSIONING-DELEGATE-CONFIRMED-FIDELITY-PLAYBACK" in text
    assert "Answering never copies or pushes as a side effect" in text
