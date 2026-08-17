"""SUPERVISED-SMOKE OVERRIDE — historical compatibility and neutral-seam tests.

AMENDED 2026-07-24: production assemblers always select blinders observe/enforce, so this module's
plain-registry spawns are structural compatibility seams, not the current production posture.

THE COUPLING THIS KNOB LOUDLY BREAKS (and why that is allowed exactly once): SECURITY.md
constraint 4 ties skip-permissions to the jail — "skip-permissions INSIDE the jail … containment
bounds the blast radius" — so ``pin_and_open`` adds ``--dangerously-skip-permissions`` ONLY when a
containment block is resolved. The first supervised live run is a small UNJAILED smoke run, and the
user explicitly decided (2026-06-10): "Unjailed + dangerously skip permissions. It is a small run,
the risk of something catastrophic happening is minimal."

The override is an EXPLICIT, OPT-IN seam — never a silent decoupling:

  * the env var ``HARNESS_UNJAILED_SKIP_PERMISSIONS=1`` (STRICTLY "1") is read at the launch-path
    assemblers — ``commissioning.build_runtime`` (the genesis L1 config) and
    ``config.get_level_config`` (the ipc/outbox child-spawn resolver) — NEVER inside the adapter;
  * the posture rides the LevelConfig (``unjailed_skip_permissions``), default ``False``;
  * the adapter honors it ONLY from level_config — a per-spawn BRIEF cannot grant it (§2.5b:
    an override may TIGHTEN, never RELAX; an injected brief must not self-escalate);
  * the posture is JOURNALED: ``SpawnResult.permission_posture`` + the STEP4 binding stamp
    ``permission_posture: unjailed-skip-permissions-override`` (SECURITY.md §4.3 — auditable the
    same way OAuth-only is);
  * default OFF does not add the skip-permissions flag; other explicit policy flags remain visible;
  * RETIREMENT: the jail tier (REMEDIATION F9–F13) retires this knob — once the first run is
    jailed, the containment-coupled posture is the only one.
"""

from __future__ import annotations

import copy
import dataclasses
import tempfile

import pytest

import harnessd.commissioning as commissioning
import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.ledger as ledger
from harnessd.spawn import tmux as tmux_mod
from harnessd.spawn.adapters import claude_code as cca

ENV_KNOB = "HARNESS_UNJAILED_SKIP_PERMISSIONS"
OVERRIDE_POSTURE = "unjailed-skip-permissions-override"


# ===========================================================================
# Adapter-level driver — the REAL ClaudeCodeAdapter argv assembly, mock tmux
# (the same shape tests/test_cc_config.py drives).
# ===========================================================================

class _RecTmux:
    def __init__(self):
        self.opened = []

    def build_pane_argv(self, env, argv):
        return tmux_mod.build_pane_argv(env, argv)

    def server_env(self):
        return {}

    def create_detached(self, name, pane_argv, env, cwd=None):
        self.opened.append((name, list(pane_argv), dict(env)))
        return f"{name}:0.0"


def _spawn(level_config, containment=None, brief_extra=None):
    """Drive the real adapter with/without a containment block; return the SpawnResult."""
    adapter = cca.ClaudeCodeAdapter(tmux=_RecTmux())
    env = {
        "CLAUDE_CONFIG_DIR": tempfile.mkdtemp(),
        "CLAUDE_CODE_OAUTH_TOKEN": "t",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    brief = {"role_variant": getattr(level_config, "role_variant", "L1")}
    if brief_extra:
        brief.update(brief_extra)
    if containment is not None:
        brief["containment_profile"] = containment
        env.update({"CLAUDE_CODE_TMPDIR": "/tmp/x", "HOME": "/tmp/home"})
    return adapter.pin_and_open(brief, level_config, "proj/w#exec", env)


class _PlainLC:
    """The minimal unjailed level config (no knob field at all — the pre-knob shape)."""

    role_variant = "L1"
    level = "L1"
    reasoning_effort = "high"


class _PlainLowerProducerLC:
    """Minimal lower producer config; keeps the native-Agent fence independent of the knob."""

    role_variant = "L2"
    level = "L2"
    reasoning_effort = "high"


def _knob_on_level_config(level="L1"):
    """A REAL LevelConfig with the override set (the shape commissioning produces knob-on)."""
    return dataclasses.replace(
        config.LevelConfig.for_level(level), unjailed_skip_permissions=True
    )


def _containment_block():
    wr = tempfile.mkdtemp()
    return {
        "WORKROOT": wr,
        "TMPDIR": wr + "/tmp",
        "CONFIG": tempfile.mkdtemp(),
        "HOME": "/tmp/home",
        "READ_DENY_ROOT": "",
    }


# ===========================================================================
# (1) DEFAULT OFF — absent knob adds no skip-permissions flag.
# ===========================================================================

def test_levelconfig_field_defaults_off_everywhere():
    """The knob field exists on LevelConfig and is False on EVERY registry seat (default OFF)."""
    for level in ("L1", "L2", "L3", "L4", "L5"):
        lc = config.LevelConfig.for_level(level)
        assert lc.unjailed_skip_permissions is False, (
            f"LevelConfig.unjailed_skip_permissions must default OFF — {level} carries True"
        )


def _strip_session_id(argv):
    """Drop the per-SPAWN ``--session-id <uuid>`` pair — the one sanctioned per-spawn argv
    difference (it pins CC's transcript file to the recorded session_uuid; 2026-06-11 live-run
    fix). Identity pins compare everything else byte-for-byte."""
    argv = list(argv)
    i = argv.index("--session-id")
    argv = argv[:i] + argv[i + 2:]
    # identity auto-load (2026-06-12): the composed per-spawn system-prompt path is the second
    # sanctioned per-spawn difference — pin its shape, strip the pair.
    i = argv.index("--system-prompt-file")
    assert argv[i + 1].endswith(".identity-prompt.md")
    return argv[:i] + argv[i + 2:]


def test_default_unjailed_argv_adds_no_skip_permissions_flag():
    """Knob absent -> the unjailed argv does not gain --dangerously-skip-permissions."""
    res = _spawn(_PlainLC())
    cc = str(cca._harness_root() / cca._PINNED_CC)
    stripped = _strip_session_id(res.argv)
    assert stripped[0] == cc
    assert "--dangerously-skip-permissions" not in stripped, (
        "DEFAULT OFF must not add the unjailed skip-permissions flag; got "
        f"{list(res.argv)!r}"
    )

    lower = _strip_session_id(_spawn(_PlainLowerProducerLC()).argv)
    assert "--disallowed-tools" in lower and "Agent" in lower, (
        "the producer native-Agent fence is an independent policy flag and should remain visible"
    )


def test_knob_off_explicit_false_adds_no_flag():
    """An explicit unjailed_skip_permissions=False level config behaves like the default."""
    lc = config.LevelConfig.for_level("L1")
    res = _spawn(lc)
    assert "--dangerously-skip-permissions" not in res.argv


# ===========================================================================
# (2) KNOB ON — the unjailed argv gains the flag, loudly journaled.
# ===========================================================================

def test_knob_on_unjailed_argv_carries_flag():
    res = _spawn(_knob_on_level_config())
    assert "--dangerously-skip-permissions" in res.argv, (
        "the USER-APPROVED override (unjailed_skip_permissions=True on level_config) must add "
        "--dangerously-skip-permissions to the UNJAILED argv — the supervised-smoke seam"
    )


def test_knob_on_unjailed_posture_is_journaled_on_spawn_result():
    res = _spawn(_knob_on_level_config())
    assert res.permission_posture == OVERRIDE_POSTURE, (
        "the override must stamp SpawnResult.permission_posture = "
        f"{OVERRIDE_POSTURE!r} (SECURITY.md §4.3: the posture is auditable, never silent) — "
        f"got {res.permission_posture!r}"
    )


def test_knob_on_keeps_env_floor_exact_four_vars():
    """The override changes ONLY the permission flag — the unjailed 4-var env floor is untouched."""
    res = _spawn(_knob_on_level_config())
    assert set(res.env) == {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
    }


# ===========================================================================
# (3) JAILED BEHAVIOR UNCHANGED — knob on or off, the jailed argv is the same.
# ===========================================================================

def test_jailed_argv_unchanged_by_knob():
    jailed_off = _spawn(config.LevelConfig.for_level("L1"), containment=_containment_block())
    jailed_on = _spawn(_knob_on_level_config(), containment=_containment_block())
    assert jailed_off.argv.count("--dangerously-skip-permissions") == 1
    assert jailed_on.argv.count("--dangerously-skip-permissions") == 1, (
        "the knob must NOT double-add the flag on a JAILED spawn — jailed behavior is unchanged"
    )
    assert _strip_session_id(jailed_on.argv) == _strip_session_id(jailed_off.argv)


def test_jailed_posture_is_the_constraint4_coupling():
    res = _spawn(config.LevelConfig.for_level("L1"), containment=_containment_block())
    assert res.permission_posture == "jailed-skip-permissions", (
        "a JAILED spawn journals the constraint-4 coupled posture, never the override"
    )


def test_unjailed_default_posture_is_prompting():
    res = _spawn(_PlainLC())
    assert res.permission_posture == "unjailed-prompting", (
        "an unjailed spawn WITHOUT the knob journals the prompting posture (today's behavior)"
    )


# ===========================================================================
# (4) THE BRIEF CANNOT GRANT THE OVERRIDE — config-layer only (§2.5b: a
# per-spawn override may TIGHTEN, never RELAX; an injected brief must not
# self-escalate to auto-approve).
# ===========================================================================

def test_brief_cannot_grant_override():
    res = _spawn(_PlainLC(), brief_extra={"unjailed_skip_permissions": True})
    assert "--dangerously-skip-permissions" not in res.argv, (
        "a per-spawn BRIEF must NOT be able to grant the unjailed skip-permissions override — "
        "the knob rides ONLY the config-layer LevelConfig (§2.5b: relax never comes from the brief)"
    )


# ===========================================================================
# (5) THE ENV-VAR READ — commissioning.build_runtime (genesis L1) and
# config.get_level_config (the ipc/outbox child-spawn resolver). STRICTLY "1".
# ===========================================================================

def test_commissioning_default_off(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_KNOB, raising=False)
    rt = commissioning.build_runtime(runtime_root=tmp_path, oauth_token="t")
    assert rt.config.level_config.unjailed_skip_permissions is False


def test_commissioning_env_knob_on(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_KNOB, "1")
    rt = commissioning.build_runtime(runtime_root=tmp_path, oauth_token="t")
    assert rt.config.level_config.unjailed_skip_permissions is True, (
        f"{ENV_KNOB}=1 must carry the override onto the genesis L1 level_config "
        "(commissioning.build_runtime is the launch-path assembler that reads the knob)"
    )


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "ON"])
def test_commissioning_env_knob_strictly_one(monkeypatch, tmp_path, value):
    """Only the EXACT string "1" enables the override — a loud, explicit opt-in, never fuzzy."""
    monkeypatch.setenv(ENV_KNOB, value)
    rt = commissioning.build_runtime(runtime_root=tmp_path, oauth_token="t")
    assert rt.config.level_config.unjailed_skip_permissions is False, (
        f"{ENV_KNOB}={value!r} must NOT enable the override — the opt-in is strictly '1'"
    )


def test_get_level_config_honors_env_knob_for_child_spawns(monkeypatch):
    """ipc.py / outbox.py resolve child level configs via config.get_level_config — the same
    daemon process the operator launched with the knob — so children carry the same posture
    (the smoke run is L1->L5 end-to-end)."""
    monkeypatch.setenv(ENV_KNOB, "1")
    assert config.get_level_config("L3").unjailed_skip_permissions is True
    monkeypatch.delenv(ENV_KNOB)
    assert config.get_level_config("L3").unjailed_skip_permissions is False


def test_env_knob_never_mutates_the_registry(monkeypatch):
    """The override is applied on a per-call COPY — the shared LEVEL_CONFIGS singletons and the
    pure for_level accessor stay pristine (no cross-run leak once the env var is unset)."""
    monkeypatch.setenv(ENV_KNOB, "1")
    config.get_level_config("L2")
    assert config.LEVEL_CONFIGS["L2"].unjailed_skip_permissions is False
    assert config.LevelConfig.for_level("L2").unjailed_skip_permissions is False


# ===========================================================================
# (6) THE STEP4 JOURNAL — a PRODUCTION chokepoint spawn (real chokepoint, real
# adapter, mock tmux) with the knob on stamps permission_posture into the
# BINDING, so the override is greppable in the ledger (SECURITY.md §4.3).
# ===========================================================================

NODE = "proj/widget#exec"


@pytest.fixture
def runtime(tmp_path):
    from harnessd.spawn import chokepoint

    previous_root = ledger.RUNTIME_ROOT
    previous_adapter = getattr(chokepoint, "ADAPTER", None)
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous_root
        chokepoint.set_adapter(previous_adapter)


def _seed_planned_node():
    token = fencing.mint_owner_token(NODE, "subagent-aaaa1111", "sess-uuid-seed-0001", 1)
    rec = {
        "node_address": NODE,
        "parent_address": "proj#exec",
        "level": "L3",
        "subagent_id": "subagent-aaaa1111",
        "session_uuid": "sess-uuid-seed-0001",
        "state": "planned",
        "generation": 0,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "spec_pointer": "design/intent-spec.md",  # E1 fixture completion
        "frozen_acceptance_ref": "acceptance.md",  # E1 fixture completion
        "liveness_state": "claimed",
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": None,
    }
    ledger.write_binding({NODE: copy.deepcopy(rec)}, _lock_held=True)
    return token


def test_structural_unjailed_override_spawn_journals_posture_in_binding(runtime):
    from harnessd.spawn import chokepoint

    tmux = _RecTmux()
    chokepoint.set_adapter(cca.ClaudeCodeAdapter(tmux=tmux))
    token = _seed_planned_node()

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_knob_on_level_config("L3"),
    )
    assert getattr(result, "ok") is True, f"the production spawn must succeed — got {result!r}"

    # The pane really is UNJAILED (bare env -i) AND auto-approving (the override flag).
    _name, launch_argv, _env = tmux.opened[0]
    assert launch_argv[:2] == ["env", "-i"], "the override spawn stays the bare env -i (UNJAILED)"
    assert "--dangerously-skip-permissions" in launch_argv

    binding = ledger.read_binding(NODE)
    assert binding.get("permission_posture") == OVERRIDE_POSTURE, (
        "STEP4 must stamp permission_posture into the BINDING so the user-approved override is "
        f"greppable in the ledger (SECURITY.md §4.3) — got {binding.get('permission_posture')!r}"
    )
