"""JAIL-WIRING increment — historical frozen acceptance plus current production regressions.

AMENDED 2026-07-24: real production assemblers now always stamp blinders observe/enforce. Tests
that pass a plain registry ``LevelConfig`` exercise the intentionally neutral structural/dry-run
seam only; they no longer describe the production launch posture.

THE HISTORICAL GAP this increment closed (design/SECURITY.md §2.3/§2.5a/§7 + the DEFERRED-REGISTER
"JAIL WIRING OWED" entry): the jail MODULE (harnessd/spawn/sandbox.py — render_profile + wrap +
cache_redirect_env + resolve_containment) is BUILT + REAL-sandbox-verified + gate-1-live-confirmed,
and the ADAPTER SEAM (claude_code.pin_and_open) already jails the pane WHEN the brief carries a
``containment_profile`` block. At the time, nothing in the production spawn path PRODUCED that block — the v1
chokepoint is structural (placeholder env, "daemon resolves at boot"), so ``_resolve_containment``
always returns None and a real chokepoint spawn hands tmux a BARE ``env -i`` pane — UNJAILED. This
increment wires the chokepoint to RESOLVE the containment block (``sandbox.resolve_containment``),
ATTACH it to the brief, and MERGE the §2.3 cache-redirect env, so the production pane is
sandbox-exec-wrapped.

TWO real integration facts the wiring MUST handle (both named in the "JAIL WIRING OWED" register
entry, 2026-06-06):

  (1) BRIEF-SHAPE BUG (existing, untested). ``brief.assemble_neutral`` returns a
      ``NeutralContract`` DATACLASS (no ``.get``), but the adapter reads it via
      ``(neutral_brief or {}).get("role_variant")`` / ``.get("containment_profile")``. In
      PRODUCTION (real chokepoint -> real adapter, where the brief is a NeutralContract) those
      ``.get`` calls raise ``AttributeError``. The existing adapter tests pass only because they
      hand the adapter a DICT brief. Fix: the adapter must read brief fields TOLERANTLY — a helper
      that does ``dict.get`` for a dict and ``getattr`` for the dataclass — so BOTH a dict brief
      and a ``NeutralContract`` work.

  (2) CONTAINMENT PRODUCTION. The chokepoint must, on the production path (a spawn that REQUESTS
      containment — carried on ``level_config`` per §2.5a, "the per-spawn containment_profile is set
      by the parent/L1 at the chokepoint, carried in level_config"), call
      ``sandbox.resolve_containment(node_address, runtime_root=ledger.RUNTIME_ROOT,
      config_dir=<resolved>, home=<resolved>)``, attach the resolved §2.5a block to the brief
      (``dataclasses.replace`` onto the NeutralContract's new ``containment_profile`` field), and
      MERGE ``sandbox.cache_redirect_env(block["WORKROOT"])`` into the spawn env. On a spawn that
      does NOT request containment (the v1 structural chokepoint — the path the Increment-14
      integration-B test and the Increment-9 adapter dry-run tests exercise), NO containment is
      produced: the pane stays the bare ``env -i`` isolator with EXACTLY the 4 isolation vars —
      UNJAILED.

  WHY THE TRIGGER IS A PER-SPAWN REQUEST, NOT "RUNTIME_ROOT ALONE" (load-bearing, keeps the 562
  green): the Increment-14 integration-B suite (test_integration_b.py) drives the REAL chokepoint
  with ``ledger.RUNTIME_ROOT`` SET and asserts the production pane is a BARE ``env -i`` with EXACTLY
  the 4 isolation vars (the v1 structural chokepoint). If the wiring jailed on RUNTIME_ROOT-presence
  ALONE it would break those frozen tests. So jailing is gated on an EXPLICIT per-spawn containment
  REQUEST (a ``level_config`` opt-in, §2.5a) AND the runtime root (needed to compute WORKROOT). The
  register's resolution — "the concrete spawn layer … must call resolve_containment(…), attach the
  block, merge cache_redirect_env" — is exactly this explicit production-resolution step, not a
  retroactive change to the structural chokepoint's contract.

BIAS TO REAL (Lesson 7): the production-path tests drive the REAL chokepoint.claim_and_spawn +
the REAL brief.assemble_neutral + the REAL ClaudeCodeAdapter.pin_and_open argv/env assembly +
the REAL sandbox.resolve_containment/render_profile/wrap against a REAL on-disk runtime root,
mocking ONLY ``tmux.create_detached`` (captured to read the pane vector). sandbox/brief are NEVER
mocked. The actual sandbox-exec ENFORCEMENT is already pinned in test_sandbox.py; THIS suite pins
that the production spawn ACTUALLY APPLIES it.

Authoritative sources (grounded, not recalled — Lesson 4):
  * design/SECURITY.md §2.3 (the profile + the cache-redirect env), §2.5a (the containment block
    shape + "the per-spawn containment_profile is set by the parent/L1 at the chokepoint, carried
    in level_config"), §7 (control 2 + 3: the write-jail + secret-scrub wire at the DAEMON §6.2
    pane-launch command — the §6.1 chokepoint).
  * working-notes/DEFERRED-REGISTER.md "JAIL WIRING OWED (2026-06-06)" — the authoritative blocker:
    the module was built + gate-1-live-verified; the then-production spawn was UNJAILED; the close-out is
    "call resolve_containment(…), attach the block to the brief, merge cache_redirect_env(WORKROOT);
    also verify _resolve_containment handles a NeutralContract, not just a dict".
  * harnessd/spawn/sandbox.py — resolve_containment / render_profile / wrap / cache_redirect_env.
  * harnessd/spawn/chokepoint.py — claim_and_spawn + _spawn_env (where the spawn is orchestrated).
  * harnessd/spawn/adapters/claude_code.py — _resolve_containment + pin_and_open + the brief reads.
  * harnessd/spawn/brief.py — NeutralContract + assemble_neutral.

LOAD-BEARING properties (each pins a mutant a wrong impl must fail on):
  * the production spawn is REALLY jailed (mutant: skip the containment production -> the pane
    vector is a bare ``env -i``, not sandbox-exec-wrapped -> caught by
    test_production_spawn_pane_is_sandbox_exec_wrapped).
  * the rendered profile jails THIS node's WORKROOT (resolve_containment-derived) with
    READ_DENY_ROOT = the runtime root (mutant: jail the wrong root / omit the cross-project deny ->
    caught by test_rendered_profile_jails_this_nodes_workroot).
  * the §2.3 cache-redirect env (NPM_CONFIG_CACHE … -> WORKROOT) is in the pane env (mutant: omit
    the merge -> the cache vars absent -> caught by test_cache_redirect_env_present_in_pane_env).
  * the adapter reads a NeutralContract brief tolerantly (mutant: keep dict-only ``.get`` -> a
    NeutralContract brief raises AttributeError -> caught by
    test_adapter_handles_neutral_contract_brief + the production path itself).
  * the DICT-brief path still works (mutant: switch to getattr-only -> a dict brief's
    role_variant/containment go unread -> caught by test_adapter_still_handles_dict_brief).
  * the no-containment spawn (the v1 structural chokepoint) stays UNJAILED (mutant: always jail ->
    the Increment-14 integration-B + Increment-9 dry-run bare-``env -i`` tests break -> caught by
    test_unrequested_containment_production_spawn_stays_unjailed +
    test_dry_run_no_containment_stays_unjailed_bare_env_i).
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import os
import subprocess

import pytest

import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.ledger as ledger
from harnessd.spawn import brief as brief_mod
from harnessd.spawn import sandbox
from harnessd.spawn import tmux as tmux_mod
from harnessd.spawn.adapters import claude_code as cc_mod


# ===========================================================================
# Module-under-wiring accessor (the chokepoint already exists — Increment 10 —
# but the JAIL-WIRING is owed; these tests RED until it lands).
# ===========================================================================

def _chokepoint():
    return importlib.import_module("harnessd.spawn.chokepoint")


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to a REAL on-disk tmp tree so the
# REAL executor's pathless ledger calls + the REAL EX lock land under the test
# tree, AND so the production jail path can compute a real WORKROOT off it.
# Restores the prior value + the adapter seam (no cross-test leak).
# ===========================================================================

@pytest.fixture
def runtime(tmp_path):
    chokepoint = _chokepoint()
    previous_root = ledger.RUNTIME_ROOT
    previous_adapter = getattr(chokepoint, "ADAPTER", None)
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous_root
        if hasattr(chokepoint, "set_adapter"):
            chokepoint.set_adapter(previous_adapter)


# ===========================================================================
# Seeding helpers — write a binding DIRECTLY through the REAL ledger (the §2.6
# lock-held seeding path the suite uses across Increment 10).
# ===========================================================================

NODE = "proj/widget#exec"
PARENT = "proj#exec"
SUBAGENT = "subagent-aaaa1111"
SESSION = "sess-uuid-seed-0001"


def _binding(
    *,
    node_address=NODE,
    parent_address=PARENT,
    state="planned",
    generation=0,
    lease_epoch=1,
    subagent_id=SUBAGENT,
    session_uuid=SESSION,
):
    token = fencing.mint_owner_token(node_address, subagent_id, session_uuid, lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": "L3",
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "spec_pointer": "design/intent-spec.md",  # E1 fixture completion
        "frozen_acceptance_ref": "acceptance.md",  # E1 fixture completion
        "liveness_state": "claimed",
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": None,
    }
    return rec, token


def _seed(bindings):
    ledger.write_binding(
        {b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True
    )


def _fresh_level_config():
    """A FRESH LevelConfig instance for L3 (NOT the LEVEL_CONFIGS singleton).

    ``config.LevelConfig.for_level`` returns a SHARED registry singleton; mutating it (to carry the
    containment request) would leak across tests. We construct a fresh instance so the request
    carrier never touches the shared registry.
    """
    return config.LevelConfig(
        level="L3",
        model="opus-4.8",
        runtime="claude-code",
        role_variant="L3",
        tool_manifest=("read", "write", "edit", "bash", "task"),
    )


def _plain_level_config():
    """The v1 structural LevelConfig — NO containment request (the unjailed structural chokepoint).

    This is the EXACT shape the Increment-14 integration-B suite spawns with; a production spawn
    using it must stay UNJAILED (bare ``env -i``, exactly the 4 isolation vars).
    """
    return _fresh_level_config()


def _jail_level_config():
    """A LevelConfig REQUESTING containment (§2.5a: the per-spawn containment_profile carried on
    level_config). The chokepoint sees the request and PRODUCES the block via
    ``sandbox.resolve_containment(node_address, runtime_root=ledger.RUNTIME_ROOT, …)``.

    The request is the §2.5a opt-in flag (``containment``=True). We set it on a FRESH LevelConfig
    instance (never the shared registry singleton) so the production trigger is an EXPLICIT per-spawn
    request — NOT mere RUNTIME_ROOT presence (which the structural integration-B spawn also has). The
    implementer wires the chokepoint to honor a truthy ``level_config.containment`` (the §2.5a
    containment_profile request) by resolving + attaching + merging.
    """
    base = _fresh_level_config()
    # LevelConfig is a frozen dataclass; carry the request as an extra attribute the chokepoint reads
    # (``getattr(level_config, "containment", False)``). object.__setattr__ bypasses the frozen guard
    # for THIS fresh instance only — the production daemon supplies a level_config that already
    # carries the §2.5a containment_profile request.
    object.__setattr__(base, "containment", True)
    return base


# ===========================================================================
# The REAL ClaudeCodeAdapter wired to a CAPTURING tmux whose create_detached is
# the ONLY mock (Lesson 7: mock the tmux exec, drive everything else real). It
# records the (session_name, launch_argv, env) the chokepoint+adapter actually
# hand tmux — the pane VECTOR we assert is sandbox-exec-wrapped.
# ===========================================================================

class CapturingTmux:
    """A REAL build_pane_argv (so the adapter assembles the pane EXACTLY as in production) +
    a create_detached that ONLY records — never execs (the single mocked boundary).

    server_env is clean (no leaked key) so the OAuth-isolation gate passes; capture_pane /
    list_targets / kill are inert stubs the adapter never needs in this path.
    """

    def __init__(self):
        self.created = []

    def build_pane_argv(self, env, argv):
        # The SAME from-empty isolator the production wrapper builds (delegated to the real seam
        # so the assembled pane is byte-for-byte what production assembles).
        return tmux_mod.build_pane_argv(env, argv)

    def create_detached(self, session_name, pane_argv, env, cwd=None):
        self.created.append((session_name, list(pane_argv), dict(env)))
        # Post-F18 contract (mirrors the real wrapper): return the CANONICAL live target.
        return f"{session_name}:0.0"

    def server_env(self):
        return {}

    def capture_pane(self, session_name):
        return ""

    def list_targets(self):
        return {}

    def kill(self, session_name):
        pass


def _install_real_adapter(chokepoint, tmux):
    """Inject the REAL ClaudeCodeAdapter (wired to the capturing tmux) into the chokepoint.

    The §2.11 frozen signature carries no adapter param, so the adapter is a module-level
    injectable (set_adapter / ADAPTER) — the production daemon wires the concrete fill the same way.
    THIS is the bias-to-real seam: the chokepoint drives the REAL adapter argv/env assembly, not a
    fake recorder, so the production jail path is genuinely exercised.
    """
    try:
        adapter = cc_mod.ClaudeCodeAdapter(tmux=tmux)
    except TypeError:  # tolerate an attribute-injection shape
        adapter = cc_mod.ClaudeCodeAdapter()
        adapter.tmux = tmux

    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(adapter)
    elif hasattr(chokepoint, "ADAPTER"):
        chokepoint.ADAPTER = adapter
    else:  # pragma: no cover - the seam exists (Increment 10)
        raise AssertionError(
            "chokepoint exposes no adapter-injection seam (set_adapter / ADAPTER) — the §2.11 "
            "frozen signature injects the adapter like ledger.RUNTIME_ROOT"
        )
    return adapter


def _captured_pane(tmux):
    """The pane VECTOR the chokepoint+adapter handed tmux.create_detached (the launch argv)."""
    assert tmux.created, (
        "create_detached must have been called — the production spawn must open an actor"
    )
    _session, launch_argv, _env = tmux.created[0]
    return launch_argv


def _captured_env(tmux):
    """The pane ENV the chokepoint+adapter handed tmux.create_detached."""
    assert tmux.created, "create_detached must have been called"
    _session, _launch_argv, env = tmux.created[0]
    return env


def _is_sandbox_exec_wrapped(launch_argv) -> bool:
    """True iff the pane vector is the §7.1 ``sandbox-exec -f <profile.sb> env -i … claude …`` form.

    The §2.3/§7.1 jailed pane: the seatbelt prefix is the OUTSIDE of the launch command — head
    ``sandbox-exec``, then ``-f <profile-file>``, then the from-empty ``env -i`` isolator as the tail
    head. A bare ``env -i …`` head (no sandbox-exec) is the UNJAILED structural-spawn vector this
    distinguishes.
    """
    if not launch_argv or len(launch_argv) < 3:
        return False
    if not str(launch_argv[0]).endswith("sandbox-exec"):
        return False
    if launch_argv[1] != "-f":
        return False
    # The wrapped pane tail still begins with the from-empty isolator (env -i …).
    tail = launch_argv[3:]
    return tail[:2] == ["env", "-i"]


def _expected_workroot(runtime_root) -> str:
    """The resolve_containment-derived WORKROOT for THIS node, realpath-canonicalized (§2.4).

    sandbox.resolve_containment derives the NESTED node dir (addressing.node_dir = nodes/<path>);
    render_profile canonicalizes. We compute the same so the test pins the RIGHT node's jail.
    """
    import harnessd.addressing as _addressing
    return os.path.realpath(str(_addressing.node_dir(NODE, str(runtime_root))))


# A real CONFIG dir so the adapter's _write_profile can write the rendered .sb to disk. The chokepoint
# resolves containment with config_dir=<resolved>; we expose a real dir via the env override the
# wiring reads, and the adapter's _write_profile also falls back to the containment CONFIG/TMPDIR
# (both under the real runtime root) — either way the write lands on disk.
def _real_config_dir(runtime_root):
    config_dir = os.path.join(str(runtime_root), "cc-config")
    os.makedirs(config_dir, exist_ok=True)
    return config_dir


# ===========================================================================
# (a) THE HEADLINE — a PRODUCTION chokepoint spawn (real runtime root, real brief,
# real adapter, mock tmux) that REQUESTS containment hands tmux.create_detached a
# SANDBOX-EXEC-WRAPPED pane vector. The pane is JAILED — NOT the bare ``env -i`` of
# the unjailed structural spawn.
#
# Mutant killed: skip the containment production in the chokepoint -> the adapter
# resolves no block -> the pane is a bare ``env -i`` -> this test FAILS.
# ===========================================================================

def test_production_spawn_pane_is_sandbox_exec_wrapped(runtime, monkeypatch):
    chokepoint = _chokepoint()
    tmux = CapturingTmux()
    _install_real_adapter(chokepoint, tmux)

    monkeypatch.setenv("HARNESS_CC_CONFIG_DIR", _real_config_dir(runtime))

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_jail_level_config(),
    )

    assert getattr(result, "ok") is True, (
        "the production spawn must succeed (a real brief + real adapter + real sandbox render) — "
        f"got {result!r}"
    )

    launch_argv = _captured_pane(tmux)
    assert _is_sandbox_exec_wrapped(launch_argv), (
        "JAIL WIRING: a PRODUCTION chokepoint spawn that REQUESTS containment must hand tmux a "
        "SANDBOX-EXEC-WRAPPED pane vector `[sandbox-exec, -f, <profile.sb>, env, -i, …claude…]` — "
        "the pane is JAILED. A bare `env -i …` head means nothing PRODUCED the containment block "
        f"(the owed gap), so the production spawn is UNJAILED. Got: {launch_argv!r}"
    )


def test_real_production_default_observe_carries_jail_and_binding_posture(
    runtime, monkeypatch
):
    """Regression for the discovered designed-but-never-production-wired containment gap."""
    chokepoint = _chokepoint()
    tmux = CapturingTmux()
    _install_real_adapter(chokepoint, tmux)
    monkeypatch.delenv(config.BLINDERS_MODE_ENV, raising=False)
    monkeypatch.setenv("HARNESS_CC_CONFIG_DIR", _real_config_dir(runtime))
    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=config.get_level_config("L3"),
    )

    assert getattr(result, "ok") is True
    launch_argv = _captured_pane(tmux)
    assert _is_sandbox_exec_wrapped(launch_argv)
    profile_path = launch_argv[2]
    profile_text = open(profile_path).read()
    assert "BLINDERS OBSERVE" in profile_text
    assert "(deny file-write*)" in profile_text

    # This is the exact profile produced by the real production chokepoint+adapter path, applied by
    # the real seatbelt binary.  A write beside (not inside) the node workroot must stay blocked.
    forbidden = runtime / "production-jail-must-block"
    denied = subprocess.run(
        [
            sandbox.SANDBOX_EXEC,
            "-f",
            profile_path,
            "/usr/bin/touch",
            str(forbidden),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert denied.returncode != 0
    assert not forbidden.exists()

    pane_env = _captured_env(tmux)
    assert pane_env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert pane_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert pane_env["XDG_CONFIG_HOME"].startswith(_expected_workroot(runtime))
    live = ledger.read_binding(NODE)
    posture = live.get("containment_posture")
    assert posture["mode"] == "observe"
    assert posture["profile_sha256"]
    assert posture["degraded"] is False


# ===========================================================================
# (b) THE RENDERED PROFILE jails THIS node's WORKROOT (resolve_containment-derived)
# with READ_DENY_ROOT = the runtime root. We read the .sb FILE the seatbelt -f points
# at off disk and assert it jails the right WORKROOT + the cross-project read-deny.
#
# Mutant killed: jail the wrong root (a hardcoded path / a different node) OR omit the
# cross-project read-deny -> the on-disk profile fails these asserts.
# ===========================================================================

def test_rendered_profile_jails_this_nodes_workroot(runtime, monkeypatch):
    chokepoint = _chokepoint()
    tmux = CapturingTmux()
    _install_real_adapter(chokepoint, tmux)

    monkeypatch.setenv("HARNESS_CC_CONFIG_DIR", _real_config_dir(runtime))

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_jail_level_config(),
    )
    assert getattr(result, "ok") is True

    launch_argv = _captured_pane(tmux)
    assert _is_sandbox_exec_wrapped(launch_argv), "precondition: the pane must be jailed"

    # The seatbelt profile FILE the pane reads (`sandbox-exec -f <profile.sb>`).
    profile_path = launch_argv[2]
    assert os.path.exists(profile_path), (
        f"the seatbelt -f profile file must exist on disk for the pane to read: {profile_path!r}"
    )
    profile_text = open(profile_path).read()

    # The containment THIS node's WORKROOT is resolve_containment-derived: runtime_root joined with
    # the collapsed node address. The profile must write-jail (and read-reallow) exactly THAT root,
    # realpath-canonicalized (§2.4) — never some other node's / a hardcoded root.
    expected_workroot = _expected_workroot(runtime)
    assert f'(subpath "{expected_workroot}")' in profile_text, (
        "the rendered profile must jail THIS node's WORKROOT (resolve_containment: runtime_root + "
        f"collapsed address), realpath-canonicalized. Expected subpath {expected_workroot!r} in the "
        f"profile, got:\n{profile_text}"
    )
    # Pass 6 retires the coarse whole-runtime deny.  The strong policy globally denies local reads,
    # then reopens own subtree + exact parent/sibling direct surfaces + declared/runtime paths.
    assert "BLINDERS ENFORCE" in profile_text
    assert "(deny file-read*)" in profile_text
    assert f'(subpath "{expected_workroot}")' in profile_text
    # And it must really be the §2.3 jail — the write-jail deny-all + keychain mach-deny are present.
    assert "(deny file-write*)" in profile_text, "the rendered profile must be the §2.3 write-jail"
    assert "(deny mach-lookup" in profile_text, "the §2.3 keychain mach-deny must be present"


# ===========================================================================
# (c) THE §2.3 CACHE-REDIRECT ENV (NPM_CONFIG_CACHE etc. -> WORKROOT) is in the pane env.
# Without it a real npm/go/cargo build hard-fails EPERM on its first fetch under the jail.
#
# Mutant killed: omit the cache_redirect_env merge in the chokepoint -> the cache vars are
# absent from the pane env -> caught.
# ===========================================================================

def test_cache_redirect_env_present_in_pane_env(runtime, monkeypatch):
    chokepoint = _chokepoint()
    tmux = CapturingTmux()
    _install_real_adapter(chokepoint, tmux)

    monkeypatch.setenv("HARNESS_CC_CONFIG_DIR", _real_config_dir(runtime))

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_jail_level_config(),
    )
    assert getattr(result, "ok") is True

    pane_env = _captured_env(tmux)

    # The §2.3 cache-redirect vars must be present AND point INTO this node's WORKROOT.
    expected_workroot = _expected_workroot(runtime)
    for key in ("NPM_CONFIG_CACHE", "PIP_CACHE_DIR", "GOMODCACHE", "GOCACHE",
                "CARGO_HOME", "YARN_CACHE_FOLDER", "NUGET_PACKAGES"):
        assert key in pane_env, (
            f"§2.3 cache-redirect var {key!r} must be MERGED into the pane env by the chokepoint "
            "(else a real npm/go/cargo fetch hard-fails EPERM on first write under the jail). "
            f"pane env keys: {sorted(pane_env)!r}"
        )
        assert pane_env[key].startswith(expected_workroot), (
            f"{key} must redirect INTO this node's WORKROOT ({expected_workroot!r}); "
            f"got {pane_env[key]!r}"
        )
    # The OAuth-only isolation floor (the 4 isolation vars) is still carried under the jail.
    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER"):
        assert key in pane_env, (
            f"the 4-var isolation floor must remain in the jailed pane env; missing {key!r}"
        )
    # And NO raw API key leaked into the jailed env (the OAuth-only HARD INVARIANT still holds).
    assert "ANTHROPIC_API_KEY" not in pane_env and "OPENAI_API_KEY" not in pane_env, (
        "the jailed pane env must still carry NO raw API key (OAuth-only HARD INVARIANT)"
    )


# ===========================================================================
# (d) THE BRIEF-SHAPE BUG — the adapter handles a NeutralContract brief (a DATACLASS,
# no .get): role_variant + containment_profile are read correctly from the dataclass.
# The existing DICT-brief path still works.
#
# These exercise the ADAPTER DIRECTLY (the real ClaudeCodeAdapter.pin_and_open) with a
# REAL NeutralContract brief — the exact production shape that today raises AttributeError
# on `(neutral_brief or {}).get(...)`.
# ===========================================================================

def _iso_env():
    """The 4-var isolation env the adapter's gates accept (OAuth token present, no api key)."""
    return {
        "CLAUDE_CONFIG_DIR": "/HARNESS/.cc-pinned/config",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-xyz",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def _real_block(tmp_path):
    """A RESOLVED §2.5a containment block with REAL on-disk WORKROOT/CONFIG (render + write work)."""
    workroot = tmp_path / "wr"
    (workroot / ".tmp").mkdir(parents=True)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    return {
        "WORKROOT": str(workroot),
        "TMPDIR": str(workroot / ".tmp"),
        "CONFIG": str(cfg),
        "HOME": os.path.expanduser("~"),
        "READ_DENY_ROOT": str(tmp_path),
        "extra_read_denies": [],
        "extra_write_roots": [],
    }, workroot


def _neutral_contract(role_variant="L3", containment_profile=None):
    """Build a REAL NeutralContract via brief.assemble_neutral (the production producer).

    This is the exact dataclass the chokepoint hands the adapter in production — the shape the
    brief-shape bug trips on. Optionally attaches a containment_profile (via dataclasses.replace
    onto the new field) so the dataclass-jailed path is exercised.
    """
    lc = config.LevelConfig.for_level(role_variant if role_variant in ("L1", "L2", "L3", "L4", "L5") else "L3")
    work_node = {"workspace": "/runtime/proj-widget-exec", "spec_pointer": None, "frozen_acceptance_ref": None}
    contract = brief_mod.assemble_neutral(NODE, lc, work_node)
    if containment_profile is not None:
        contract = dataclasses.replace(contract, containment_profile=containment_profile)
    return contract


def test_neutral_contract_has_containment_profile_field():
    """NeutralContract must carry a ``containment_profile`` field (default None) — the seat the
    chokepoint attaches the resolved block onto via dataclasses.replace.

    Mutant killed: no field -> dataclasses.replace(contract, containment_profile=…) raises -> the
    wiring cannot attach the block -> caught here directly.
    """
    contract = _neutral_contract()
    assert hasattr(contract, "containment_profile"), (
        "NeutralContract must declare a ``containment_profile`` field (default None) so the "
        "chokepoint can attach the resolved §2.5a block onto the brief (dataclasses.replace)"
    )
    assert contract.containment_profile is None, (
        "the field defaults to None (the unjailed/structural shape until the chokepoint resolves a block)"
    )


def test_adapter_handles_neutral_contract_brief(tmp_path):
    """THE BRIEF-SHAPE BUG: the REAL adapter must read role_variant + containment_profile from a
    NeutralContract DATACLASS (no .get) without raising AttributeError, and JAIL the pane.

    Mutant killed: keep the dict-only ``(neutral_brief or {}).get(...)`` reads -> a NeutralContract
    brief raises AttributeError -> caught.
    """
    tmux = CapturingTmux()
    adapter = cc_mod.ClaudeCodeAdapter(tmux=tmux)

    block, workroot = _real_block(tmp_path)
    contract = _neutral_contract(role_variant="L3", containment_profile=block)
    env = {**_iso_env(), **sandbox.cache_redirect_env(str(workroot))}

    result = adapter.pin_and_open(contract, _plain_level_config(), NODE, env)

    # No AttributeError raised AND the pane is jailed (the dataclass containment_profile was read).
    assert getattr(result, "ok") is True
    assert result.role_variant == "L3", (
        "role_variant must be read from the NeutralContract dataclass (getattr), not via dict.get "
        f"(which raises on a dataclass); got {result.role_variant!r}"
    )
    launch_argv = _captured_pane(tmux)
    assert _is_sandbox_exec_wrapped(launch_argv), (
        "the adapter must JAIL the pane when the NeutralContract brief carries a containment_profile "
        "(read tolerantly from the dataclass) — got a bare/unwrapped pane, so the dataclass "
        f"containment_profile was not read: {launch_argv!r}"
    )


def test_adapter_still_handles_dict_brief(tmp_path):
    """The existing DICT-brief path STILL works (the tolerant read must not break dicts).

    Mutant killed: switch to a getattr-ONLY read -> a dict brief's role_variant/containment_profile
    go unread (dict has no such attributes) -> the pane is unjailed / role_variant wrong -> caught.
    """
    tmux = CapturingTmux()
    adapter = cc_mod.ClaudeCodeAdapter(tmux=tmux)

    block, workroot = _real_block(tmp_path)
    dict_brief = {
        "node_address": NODE,
        "role_variant": "L4",
        "load_manifest": ["operational/L4/role.md"],
        "containment_profile": block,
    }
    env = {**_iso_env(), **sandbox.cache_redirect_env(str(workroot))}

    result = adapter.pin_and_open(dict_brief, _plain_level_config(), NODE, env)

    assert getattr(result, "ok") is True
    assert result.role_variant == "L4", (
        "the DICT-brief role_variant must still be read via dict.get; the tolerant helper must not "
        f"regress the dict path; got {result.role_variant!r}"
    )
    launch_argv = _captured_pane(tmux)
    assert _is_sandbox_exec_wrapped(launch_argv), (
        "a DICT brief carrying a containment_profile must still jail the pane (the tolerant read "
        f"must keep the dict path working): {launch_argv!r}"
    )


def test_adapter_unjailed_dict_brief_without_containment_stays_bare():
    """A DICT brief WITHOUT a containment_profile stays UNJAILED (the dry-run boundary).

    Pins that the tolerant read does not accidentally jail when no block is present — the
    Increment-9 dry-run invariant (bare ``env -i``, env EXACTLY the 4 isolation vars).
    """
    tmux = CapturingTmux()
    adapter = cc_mod.ClaudeCodeAdapter(tmux=tmux)

    dict_brief = {"node_address": NODE, "role_variant": "L2", "load_manifest": []}
    result = adapter.pin_and_open(dict_brief, _plain_level_config(), NODE, _iso_env())

    assert getattr(result, "ok") is True
    launch_argv = _captured_pane(tmux)
    assert launch_argv[:2] == ["env", "-i"], (
        "a brief with NO containment_profile must leave the pane the bare from-empty `env -i` "
        f"isolator — UNJAILED (the dry-run boundary the Increment-9 tests pin): {launch_argv!r}"
    )
    assert not _is_sandbox_exec_wrapped(launch_argv), (
        "no containment block -> the pane must NOT be sandbox-exec-wrapped (the unjailed dry-run)"
    )


# ===========================================================================
# (e) THE NO-CONTAINMENT v1 STRUCTURAL TEST SEAM stays unjailed — it is not production config.
# ``env -i`` pane with EXACTLY the 4 isolation vars — so the Increment-14 integration-B
# and Increment-9 adapter dry-run tests keep passing.
#
# Drives the REAL chokepoint with a PLAIN level_config (no containment request) against a
# REAL runtime root: the chokepoint must NOT produce a containment block, so the pane is a
# bare `env -i` and the env is exactly the 4 isolation vars.
#
# Mutant killed: ALWAYS jail (produce containment whenever RUNTIME_ROOT is set) -> the
# structural spawn pane is sandbox-wrapped / the env is widened -> the Increment-14
# integration-B frozen tests break -> caught here.
# ===========================================================================

def test_unrequested_containment_structural_spawn_stays_unjailed(runtime):
    chokepoint = _chokepoint()
    tmux = CapturingTmux()
    _install_real_adapter(chokepoint, tmux)

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_plain_level_config(),  # NO containment request -> the structural chokepoint
    )
    assert getattr(result, "ok") is True

    launch_argv = _captured_pane(tmux)
    assert launch_argv[:2] == ["env", "-i"], (
        "a structural spawn (no containment request) must leave the pane the bare from-empty "
        f"`env -i` isolator — UNJAILED (the Increment-14 integration-B contract): {launch_argv!r}"
    )
    assert not _is_sandbox_exec_wrapped(launch_argv), (
        "no containment request -> NO sandbox-exec wrap (the v1 structural chokepoint the "
        "integration-B tests pin); jailing on RUNTIME_ROOT-presence ALONE would break them"
    )

    # And the env is EXACTLY the 4 isolation vars (no cache-redirect merge on the structural path).
    pane_env = _captured_env(tmux)
    assert set(pane_env) == {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
        "PATH",  # LR-2 (2026-06-11): non-credential ergonomics joined the floor
    }, (
        "the structural (no-containment) pane env = the 4 isolation vars + PATH (LR-2) — the "
        f"cache-redirect merge is a containment-only step (Increment-14 contract). Got: {sorted(pane_env)!r}"
    )


def test_dry_run_no_containment_stays_unjailed_bare_env_i():
    """The adapter-level dry-run boundary: a NeutralContract with containment_profile None (the
    default) renders a BARE ``env -i`` pane — UNJAILED.

    This is the Increment-9 invariant restated against the REAL production brief shape: the
    NeutralContract default (no block) must NOT jail.
    """
    tmux = CapturingTmux()
    adapter = cc_mod.ClaudeCodeAdapter(tmux=tmux)

    contract = _neutral_contract(role_variant="L3", containment_profile=None)
    adapter.pin_and_open(contract, _plain_level_config(), NODE, _iso_env())

    launch_argv = _captured_pane(tmux)
    assert launch_argv[:2] == ["env", "-i"], (
        "a NeutralContract with no containment_profile (the default) must render a bare from-empty "
        f"`env -i` pane — UNJAILED (Increment-9 invariant on the real brief shape): {launch_argv!r}"
    )
    assert not _is_sandbox_exec_wrapped(launch_argv)
