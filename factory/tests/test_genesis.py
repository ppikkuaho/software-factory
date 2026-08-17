"""Increment 12 — FROZEN acceptance for genesis + daemon + reconcile_tick wiring
(Integration A, the boot/orchestration layer). Tests ONLY — NO implementation. RED first.

The daemon is the ROOT of the supervision-tree custody chain: it starts L1, which has no parent
agent (DAEMON §7: "L1 has no parent agent — the daemon is what starts L1"). genesis is the
first-boot sequence that establishes that custody; reconcile_tick is the continuous, edge-triggered
sweep on the timer.

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.12 — the FROZEN genesis/daemon/necro interface (transcribed below):
        genesis.run_genesis(executor, tmux, config) -> None
        daemon.boot(runtime) -> None
        daemon.poll_loop(interval_s) -> None            # ticks while live; returns at definitive idle
        necro.resume_brief(node_address) -> (ResumeArgs, DeltaBrief)
    Plus the run-scoped launchd prototype (Label, fixed request argv,
    KeepAlive={SuccessfulExit=false}, no RunAtLoad, ThrottleInterval>=10) and the LOCK-FREE status
    sidecar (status.json written WITHOUT the EX lock).
  * IMPLEMENTATION-PLAN §3 module table (genesis.py row, L63): "acquire .harnessd.lock -> write
    runtime.json -> preconditions() (OAuth health + pinned-binary hash, fail-loud) ->
    reconcile_on_restart -> if no live non-terminal L1 binding: spawn.claim_and_spawn(L1-root,
    role_variant='L1', parent=null) ... else RESUME (no double-spawn, F35)."
  * The FULL Increment-12 Done-test (L790-798): Integration A (boot+reconcile on a FAKE adapter) —
    L1 spawned in-role (dry-run) with the shared prompt base + the L1 load-manifest in the neutral
    brief; the real adapter composes the runtime `.identity-prompt.md`; owned-but-dead node necro'd; liveness
    reconstructed from the ledger; PLUS the reconcile_tick edge-trigger (one liveness CHANGE -> one
    WAL row; N steady-healthy polls -> ZERO rows).
  * Integration A spec (L623-629): start daemon on empty /runtime, assert L1 spawned in-role with the
    shared prompt base AND the L1 load-manifest delivered in the neutral brief, binding registered
    (parent_address=null, role_variant='L1');
    pre-seed an owned-but-dead node, restart, assert reconcile necros it and reconstructs liveness
    from the LEDGER (not memory); fake-tmux + dry-run adapter, no usage.
  * DAEMON §7 — the LOCKED genesis sequence (L1058-1087): (1) lock; (2) runtime.json; (3) PRECONDITION
    CHECK fail-loud, do NOT spawn on a bad precondition (OAuth credential health: absent token ->
    auth_expired; pinned-binary present+version/hash verified); (4) reconcile-on-restart; (5) if no
    live non-terminal L1 binding -> SPAWN L1 root in-role (parent_address=null, role_variant=L1) ELSE
    RESUME, do NOT double-spawn (F35).
  * DAEMON §5.2 — continuous reconciliation: the same sweep on the timer; edge-triggered (only
    state/condition CHANGES append to the run-ledger).
  * DAEMON §4.4 — the status sidecar is the ONE deliberate carve-out: best-effort, LOCK-FREE liveness
    surface written every poll; it CANNOT take the EX lock (that would serialize a non-event against
    real mutations every tick); recovery NEVER trusts it (the ledger is the truth).

BIAS TO REAL (Lesson 7): genesis drives the REAL executor + REAL on-disk ledger (tmp RUNTIME_ROOT) +
REAL reconcile_on_restart/reconcile_tick + REAL chokepoint.claim_and_spawn/resume +
REAL oauth_guard.check_credential_health. The ONLY fake is the RuntimeAdapter (records the L1
pin_and_open; dry-run, no real pane) and the minimal tmux list_targets surface (Increment 9 already
validated the real adapter+tmux against REAL tmux; this increment tests the boot ORCHESTRATION). The
launchd plist is a CONFIG artifact (asserted well-formed; no daemon process is launched). poll_loop is
driven via its SINGLE-iteration factor, NEVER an unbounded run.

NO IMPLEMENTATION here — harnessd/genesis.py and harnessd/daemon.py do not exist yet (RED until
written). The frozen reconcile/chokepoint/executor/ledger/oauth_guard modules (Increments 0-11, 450
green) are REAL and untouched.

LOAD-BEARING (each pins a mutant a wrong impl must fail on):
  * genesis preconditions FAIL-LOUD BEFORE any spawn (mutant: spawn despite an absent OAuth token ->
    caught: AuthExpired raised AND the fake adapter NEVER opened an L1 actor).
  * first-boot SPAWNS L1 IN-ROLE with the SHARED prompt base + the L1 manifest (mutant: role.md in argv /
    no manifest / a per-level system-prompt -> caught: the recorded spawn carries
    role_variant='L1' + shared system_prompt_file base + an L1 load-manifest).
  * relaunch with a LIVE L1 RESUMES, does NOT double-spawn (mutant: spawn a 2nd L1 -> caught: the fake
    adapter opens ZERO new actors for an already-live L1).
  * an owned-but-dead node is NECRO'd during boot, liveness reconstructed from the LEDGER (mutant: skip
    reconcile -> caught: the dead node stays running / no died_* stamp).
  * reconcile_tick is EDGE-TRIGGERED — ONE liveness CHANGE = ONE WAL row, N steady polls = ZERO rows
    (mutant: append every poll -> caught: WAL grows on a steady-healthy poll).
  * the launchd prototype has crash-only KeepAlive/no RunAtLoad/ThrottleInterval
    (mutant: standing residency restored or protection missing -> caught).
  * the status sidecar is written LOCK-FREE (mutant: take the EX lock to write status -> caught: the
    status write must succeed even when store.file_lock is poisoned).
"""

from __future__ import annotations

import copy
import importlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import harnessd.clock as clock
import harnessd.config as config
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.reconcile as reconcile
import harnessd.states as states
import harnessd.store as store
from harnessd.spawn import chokepoint
from harnessd.spawn.oauth_guard import AuthExpired


# ===========================================================================
# Modules-under-construction accessors (RED until they exist). Imported lazily
# so collection does not hard-crash before the modules are written — every test
# that touches them REDs with a clear ImportError/AttributeError, not a collection
# abort that hides the rest of the suite.
# ===========================================================================

def _genesis():
    return importlib.import_module("harnessd.genesis")


def _daemon():
    return importlib.import_module("harnessd.daemon")


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so every pathless REAL
# ledger/executor/reconcile/chokepoint call (read_binding / append_wal /
# write_binding / load_wal / the EX lock) lands under the test tree. Restores the
# prior value so tests do not leak runtime state. (The established suite pattern.)
# ===========================================================================

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
    """Reset the chokepoint's module-level injected adapter around every test (no cross-leak)."""
    previous = chokepoint.ADAPTER
    try:
        yield
    finally:
        chokepoint.ADAPTER = previous


# ===========================================================================
# The L1 root identity. The L1 root is the ONLY node with parent_address=null
# (DAEMON §7: "it is the only node with no parent"). The address itself is a
# genesis input (the test owns it; genesis owns the sequence) — passed on config.
# ===========================================================================

L1_ADDRESS = "L1#exec"
DEAD_LEAF = "proj/widget#exec"     # an owned-but-dead leaf to be necro'd during boot
DEAD_LEAF_PARENT = "proj#exec"


# ===========================================================================
# The FAKE RuntimeAdapter (the ONLY mock — executor + ledger + reconcile +
# chokepoint are REAL). It records every pin_and_open call so:
#   * the precondition-fail-loud test can assert len(calls) == 0 (no L1 opened),
#   * the in-role test can read role_variant / system_prompt_file / load_manifest
#     off the recorded (neutral_brief, level_config, ...) call,
#   * the no-double-spawn test can assert ZERO new opens for an already-live L1.
# Dry-run: no real pane, no model. (Increment 9 validated the real adapter+tmux.)
# ===========================================================================

def _spawn_result_cls():
    base = importlib.import_module("harnessd.spawn.adapters.base")
    return base.SpawnResult


class FakeAdapter:
    """Records pin_and_open calls; returns a happy SpawnResult carrying the spawn facts.

    ``calls`` is the list of (neutral_brief, level_config, tmux_target, env) tuples. The neutral_brief
    is the chokepoint's flattened brief dict (carries ``role_variant`` / ``system_prompt_file`` /
    ``load_manifest`` — the role-as-documents observables). A fresh session_uuid is minted per open so
    the no-double-spawn test can count opens precisely.
    """

    def __init__(self):
        self.calls = []
        self._n = 0

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        self._n += 1
        SpawnResult = _spawn_result_cls()
        return SpawnResult(
            ok=True,
            session_uuid=f"sess-l1-spawned-{self._n:04d}",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L1"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path=f"/runtime/transcripts/sess-l1-spawned-{self._n:04d}.jsonl",
            failure_class=None,
        )


def _install_adapter(fake):
    """Inject the FAKE adapter into the REAL chokepoint via its module-level seam (set_adapter/ADAPTER).

    The §2.11/§2.12 frozen signatures carry NO adapter param — the adapter is injected like
    ledger.RUNTIME_ROOT (the established precedent). genesis must spawn THROUGH this same real
    chokepoint, so whatever genesis does, the L1 open is recorded on this fake.
    """
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
        return
    chokepoint.ADAPTER = fake


# ===========================================================================
# Minimal fake tmux — the FROZEN §2.11 surface, list_targets() ONLY (reconcile +
# the sweep read it). Nothing else (Lesson 6: a runtime-not-yet-built mock).
# ===========================================================================

class FakeTmux:
    """Exposes EXACTLY list_targets() -> {tmux_target: {pane_pid, pane_dead, window_activity}}."""

    def __init__(self, targets=None):
        self._targets = dict(targets or {})

    def list_targets(self):
        return dict(self._targets)


def _target_alive(pid=4321, session_uuid=None):
    t = {"pane_pid": pid, "pane_dead": 0, "window_activity": "0"}
    if session_uuid is not None:
        t["session_uuid"] = session_uuid
    return t


# ===========================================================================
# Fake detector — drives reconcile_tick's per-node liveness verdict. The detector
# sits behind the stable liveness(node) -> {working|waiting|idle|dead} interface
# (DAEMON §5.2); a controllable fake is the right tool to script "exactly ONE
# liveness CHANGE" vs "N steady-healthy polls" deterministically.
# ===========================================================================

class FakeDetector:
    """Returns a scriptable per-node liveness verdict (an object with a ``.state`` attribute).

    ``set(node, state)`` flips the verdict the next tick reads. reconcile_tick reads
    ``detector.liveness(node).state`` and treats ``"dead"`` as the owned-but-dead trigger.
    """

    def __init__(self, default="working"):
        self._states = {}
        self._default = default

    def set(self, node_address, state):
        self._states[node_address] = state

    def liveness(self, node_address):
        return SimpleNamespace(
            state=self._states.get(node_address, self._default),
            last_progress_at=None,
        )


# ===========================================================================
# Seeding + config helpers — write REAL bindings through the REAL ledger
# (ledger.write_binding(map, _lock_held=True), the suite-wide direct-seed path).
# ===========================================================================

def _binding(
    node_address,
    *,
    level="L3",
    state="running",
    generation=4,
    lease_epoch=2,
    parent_address=None,
    session_uuid="sess-seed-0001",
    tmux_target=None,
    terminal_signal=None,
    liveness_state="working",
    gate_crossed_at=None,
    extra=None,
):
    subagent = "subagent-" + node_address.replace("/", "-").replace("#", "-")
    token = fencing.mint_owner_token(node_address, subagent, session_uuid, lease_epoch)
    b = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": subagent,
        "session_uuid": session_uuid,
        "tmux_target": tmux_target if tmux_target is not None else "harness:" + node_address,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": liveness_state,
        "terminal_signal": terminal_signal,
        "terminal_signal_at": None,
        "gate_crossed_at": gate_crossed_at,
        "paused_at": None,
    }
    if extra:
        b.update(extra)
    return b


def _seed(*bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _genesis_config(runtime_root, *, env=None, l1_address=L1_ADDRESS):
    """Assemble the third positional ``config`` for run_genesis(executor, tmux, config).

    The §2.12 signature names only ``config``; it carries the genesis inputs the LOCKED §7 sequence
    needs: the OAuth ``env`` (for oauth_guard.check_credential_health — absent token -> AuthExpired),
    the L1 root ``l1_address`` (where genesis registers+claims the root, parent_address=null), the
    ``runtime_root`` + the pinned-binary descriptor. A SimpleNamespace keeps the seam permissive — the
    test owns the inputs; genesis owns the sequence. A healthy env carries CLAUDE_CODE_OAUTH_TOKEN and
    NO raw API key (the OAuth-only invariant).
    """
    if env is None:
        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-present",
            "CLAUDE_CONFIG_DIR": str(runtime_root / ".cc-pinned/config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    return SimpleNamespace(
        env=env,
        l1_address=l1_address,
        l1_level="L1",
        runtime_root=runtime_root,
        build_id="build-test-0001",
        pinned_binary=config.PINNED_BINARY,
        level_config=config.LevelConfig.for_level("L1"),
    )


def _l1_opens(fake):
    """Count the L1 pin_and_open opens recorded on the fake (tmux_target carries the L1 address)."""
    n = 0
    for (_brief, _lvl, tmux_target, _env) in fake.calls:
        if L1_ADDRESS in str(tmux_target):
            n += 1
    return n


def _l1_brief(fake):
    """Return the neutral_brief dict of the FIRST recorded L1 open (the role-as-documents payload)."""
    for (brief, _lvl, tmux_target, _env) in fake.calls:
        if L1_ADDRESS in str(tmux_target):
            return brief
    raise AssertionError("no L1 pin_and_open was recorded on the fake adapter")


def _manifest_of(brief):
    """Best-effort extract the load-manifest list from a recorded brief (dict or object)."""
    if isinstance(brief, dict):
        return list(brief.get("load_manifest") or [])
    return list(getattr(brief, "load_manifest", []) or [])


def _role_variant_of(brief):
    if isinstance(brief, dict):
        return brief.get("role_variant")
    return getattr(brief, "role_variant", None)


def _system_prompt_of(brief):
    if isinstance(brief, dict):
        return brief.get("system_prompt_file")
    return getattr(brief, "system_prompt_file", None)


# ===========================================================================
# PRECONDITIONS FAIL-LOUD (the headline FAIL-LOUD property). An ABSENT OAuth token
# -> AuthExpired, and CRUCIALLY no L1 is spawned (the precondition gate is BEFORE
# the spawn — DAEMON §7 step 4: "fail loud, do not spawn on a bad precondition").
#
# Mutant killed: spawn L1 despite the absent token (precondition checked after, or
# not at all) -> the fake adapter records an L1 open -> this test FAILS.
# ===========================================================================

def test_genesis_preconditions_fail_loud_absent_token_no_l1_spawned(runtime):
    genesis = _genesis()
    fake = FakeAdapter()
    _install_adapter(fake)

    # An env with NO CLAUDE_CODE_OAUTH_TOKEN — the named genesis credential precondition (DAEMON §7).
    bad_env = {
        "CLAUDE_CONFIG_DIR": str(runtime / ".cc-pinned/config"),
        "DISABLE_AUTOUPDATER": "1",
    }
    cfg = _genesis_config(runtime, env=bad_env)
    tmux = FakeTmux({})

    with pytest.raises(AuthExpired):
        genesis.run_genesis(executor, tmux, cfg)

    # FAIL-LOUD BEFORE the spawn: the actor was NEVER opened (the precondition gates the spawn).
    assert _l1_opens(fake) == 0, (
        "genesis must FAIL LOUD on the absent-OAuth-token precondition BEFORE spawning L1 "
        "(DAEMON §7 step 4: do NOT spawn on a bad precondition) — the fake adapter recorded an L1 open"
    )
    # No L1 binding was driven to a live state by a spawn that should never have happened.
    l1 = ledger.read_binding(L1_ADDRESS)
    if l1 is not None:
        assert l1.get("state") not in ("spawning", "running"), (
            "no L1 may reach a live state when the credential precondition failed"
        )


def test_genesis_binds_and_journals_explicit_commissioning_playback_delegate(runtime):
    genesis = _genesis()
    fake = FakeAdapter()
    _install_adapter(fake)
    cfg = _genesis_config(runtime)
    cfg.fidelity_playback_authority = "operator-delegate"
    cfg.fidelity_playback_delegate = "commissioning-operator"
    cfg.fidelity_playback_delegation_reason = "supervised Q6 commissioning"

    genesis.run_genesis(executor, FakeTmux({}), cfg)

    binding = ledger.read_binding(L1_ADDRESS)
    assert binding["fidelity_playback_authority"] == "operator-delegate"
    assert binding["fidelity_playback_delegate"] == "commissioning-operator"
    assert (
        binding["fidelity_playback_delegation_reason"]
        == "supervised Q6 commissioning"
    )
    rows = [
        row
        for row in ledger.load_wal()
        if row.get("event") == "fidelity_playback_authority_delegated"
    ]
    assert len(rows) == 1
    assert "commissioning-operator" in rows[0]["summary"]


def test_genesis_binds_nonempty_panel_arms_on_the_run_root(runtime):
    genesis = _genesis()
    fake = FakeAdapter()
    _install_adapter(fake)
    cfg = _genesis_config(runtime)
    cfg.review_panel_arms = [
        {
            "pattern": "forge-queue/queue-core#exec",
            "axes": ["composition-interface", "risk-readiness"],
        },
        {
            "pattern": "forge-queue/persistence#exec",
            "axes": ["broad"],
        },
    ]

    genesis.run_genesis(executor, FakeTmux({}), cfg)

    assert ledger.read_binding(L1_ADDRESS)["review_panel_arms"] == cfg.review_panel_arms


def test_genesis_precondition_uses_real_oauth_guard_distinct_class(runtime):
    """The fail-loud is the REAL oauth_guard.check_credential_health raising the DISTINCT AuthExpired
    (a SpawnFailure subclass, NOT ApiKeyForbidden) — a token lapse reads as "refresh the token", not a
    fleet-wide model outage (DAEMON §6.3). Pins that genesis routes through the real positive check,
    not a home-grown string check.

    Mutant killed: genesis raises a generic Exception (or ApiKeyForbidden) on the absent token ->
    the AuthExpired-specific catch fails.
    """
    genesis = _genesis()
    _install_adapter(FakeAdapter())
    cfg = _genesis_config(runtime, env={"DISABLE_AUTOUPDATER": "1"})  # no token
    try:
        genesis.run_genesis(executor, FakeTmux({}), cfg)
    except AuthExpired as exc:
        from harnessd.spawn.oauth_guard import SpawnFailure, ApiKeyForbidden

        assert isinstance(exc, SpawnFailure), "AuthExpired must be a SpawnFailure (the spawn-failure taxonomy)"
        assert not isinstance(exc, ApiKeyForbidden), (
            "an absent OAuth token is AuthExpired, NOT ApiKeyForbidden — the two classes are distinct "
            "so a token lapse is not mistaken for a leaked raw API key"
        )
    else:
        pytest.fail("genesis must raise AuthExpired (the credential precondition) on an absent token")


# ===========================================================================
# FIRST BOOT — L1 spawned IN-ROLE (Integration A, the headline). On an EMPTY runtime
# (no L1 binding), genesis registers + spawns the L1 ROOT through the REAL chokepoint
# with:
#   * role_variant == 'L1',
#   * system_prompt_file == operational/shared/system-prompt.md (the SHARED base),
#   * an L1 load-manifest in the neutral brief,
#   * the binding registered with parent_address == null.
#
# Mutants killed:
#   * role.md/per-level prompt base (no manifest)     -> the manifest/role_variant assertions fail
#   * a per-level shared-prompt base                  -> the shared-base assertion fails
#   * parent_address != null for the root             -> the root-binding assertion fails
# ===========================================================================

def test_first_boot_spawns_l1_in_role_with_shared_prompt_and_manifest(runtime):
    genesis = _genesis()
    fake = FakeAdapter()
    _install_adapter(fake)

    cfg = _genesis_config(runtime)
    tmux = FakeTmux({})   # empty runtime: nothing live, no L1 binding yet

    genesis.run_genesis(executor, tmux, cfg)

    # Exactly ONE L1 actor opened (first boot spawns the root).
    assert _l1_opens(fake) == 1, "first boot must spawn EXACTLY one L1 root actor"

    brief = _l1_brief(fake)

    # IN-ROLE = role_variant 'L1' (the seat), delivered through the neutral contract.
    assert _role_variant_of(brief) == "L1", "the L1 root must spawn with role_variant='L1'"

    # The SHARED system-prompt base, byte-identical L1–L5 — NEVER a per-level role file.
    assert _system_prompt_of(brief) == config.SYSTEM_PROMPT_FILE == "operational/shared/system-prompt.md", (
        "L1 must use the SHARED base prompt operational/shared/system-prompt.md "
        "(the one minimal base, identical L1–L5), NOT a per-level role file"
    )

    # LOAD-MANIFEST: broader L1 docs arrive as paths the agent reads in place. The identity trio is
    # additionally auto-loaded by the adapter into the composed prompt in real Claude seats.
    manifest = _manifest_of(brief)
    assert manifest, "the L1 brief must carry a non-empty load-manifest (role-as-documents)"
    assert any("operational/L1/" in entry for entry in manifest), (
        "the L1 load-manifest must reference the L1 role docs (operational/L1/...) — the role is "
        "delivered as DOCUMENTS the agent reads in place, never as system-prompt content"
    )
    # And the role file path is NOT smuggled into the argv/system-prompt: the system prompt is the
    # shared constant (asserted above), so 'operational/L1/role.md' lives ONLY in the manifest.
    assert _system_prompt_of(brief) != "operational/L1/role.md", (
        "the per-level role file must NOT be the --system-prompt-file (role-as-documents, not -prompt)"
    )


def test_first_boot_registers_l1_root_binding_parent_null_running(runtime):
    """After first boot the L1 root binding is REGISTERED in the REAL ledger: parent_address=null (the
    only parentless node), role_variant/level L1, and driven to a live state by the real chokepoint
    (claim -> spawning -> running). Asserts on the REAL on-disk binding (bias to real).

    Mutant killed: register L1 with a non-null parent (it is NOT a child of anything) -> caught; or
    never register the root binding -> caught.
    """
    genesis = _genesis()
    _install_adapter(FakeAdapter())
    genesis.run_genesis(executor, FakeTmux({}), _genesis_config(runtime))

    l1 = ledger.read_binding(L1_ADDRESS)
    assert l1 is not None, "genesis must REGISTER the L1 root node in the binding ledger"
    assert l1.get("parent_address") in (None, ""), (
        "the L1 root is the ONLY node with no parent (DAEMON §7) — parent_address must be null"
    )
    assert l1.get("state") == "running", (
        "the spawned L1 root must reach 'running' via the real chokepoint (claim->spawning->running)"
    )
    assert not states.is_terminal(l1.get("state")), "a freshly-spawned L1 root is non-terminal"
    # The recorded spawn fact: a real session_uuid was written by STEP4 (config=intent, fact=session).
    assert l1.get("session_uuid"), "the L1 binding must record the spawned session_uuid (STEP4 fact)"


# ===========================================================================
# OWNED-BUT-DEAD NECRO'd DURING BOOT + liveness reconstructed from the LEDGER (the
# reconcile half of Integration A). Pre-seed an owned-but-dead leaf (recorded
# running, tmux GONE). genesis's reconcile_on_restart must necro it (mark dead,
# stamp died_*, bump epoch, append) — reconstructing liveness from the LEDGER +
# tmux, not from memory.
#
# Mutant killed: skip reconcile in genesis -> the dead leaf stays 'running', no
# died_* stamp, no necro WAL row.
# ===========================================================================

def test_genesis_reconcile_necros_owned_but_dead_leaf_during_boot(runtime):
    genesis = _genesis()
    _install_adapter(FakeAdapter())

    # An owned-but-dead leaf: recorded running, but tmux has NO target for it -> owned-but-dead.
    dead = _binding(
        DEAD_LEAF, level="L5", state="running", generation=4, lease_epoch=2,
        parent_address=DEAD_LEAF_PARENT, tmux_target="harness:" + DEAD_LEAF,
    )
    _seed(dead)
    wal_before = len(ledger.load_wal())

    # tmux lists NOTHING for the dead leaf (its pane is gone). L1 has no binding yet (first-boot spawn).
    tmux = FakeTmux({})
    genesis.run_genesis(executor, tmux, _genesis_config(runtime))

    rb = ledger.read_binding(DEAD_LEAF)
    assert rb is not None, "the pre-seeded dead leaf must still be present (necro'd, not deleted)"
    assert rb["state"] == "failed", (
        "genesis's reconcile-on-restart must NECRO the owned-but-dead leaf (running->failed, "
        "DIED_INFRA per §3.6) — liveness is reconstructed from the LEDGER + tmux, not trusted from memory"
    )
    assert rb.get("terminal_signal") in {"DIED_INFRA", "DIED_INFRASTRUCTURE", "DIED_METHODOLOGY"}, (
        "the necro must stamp a died_* terminal_signal (§3.6 death class)"
    )
    assert rb["lease_epoch"] > dead["lease_epoch"], "the necro must BUMP lease_epoch (fence the prior)"
    assert len(ledger.load_wal()) > wal_before, "the necro must append a run-ledger (WAL) death event"


def test_genesis_reconstructs_liveness_from_ledger_not_memory(runtime):
    """The owned-but-dead verdict is reconstructed from the on-disk LEDGER (+ tmux), not from any
    in-memory daemon state. We prove this by seeding the dead binding ONLY on disk (no daemon object
    has ever seen it) and asserting genesis still necros it.

    Mutant killed: genesis reconciles only nodes it spawned this session (memory) -> a pre-existing
    dead binding it never spawned is left running.
    """
    genesis = _genesis()
    _install_adapter(FakeAdapter())
    dead = _binding(DEAD_LEAF, level="L5", state="running", parent_address=DEAD_LEAF_PARENT)
    _seed(dead)

    genesis.run_genesis(executor, FakeTmux({}), _genesis_config(runtime))

    assert ledger.read_binding(DEAD_LEAF)["state"] == "failed", (
        "a dead binding present ONLY on disk (never in daemon memory) must still be necro'd (to "
        "'failed', §3.6 DIED_INFRA) — liveness is reconstructed from the ledger, the source of truth "
        "(DAEMON §4.4)"
    )


# ===========================================================================
# NO DOUBLE-SPAWN ON RELAUNCH — a LIVE L1 binding exists (recorded running, tmux
# alive, uuid matches). genesis must RESUME / adopt, NOT spawn a second L1 (the F35
# stable-address resume-not-double-spawn rule, DAEMON §7 step 6 ELSE-branch).
#
# Mutant killed: genesis spawns L1 unconditionally (no live-L1 check) -> the fake
# adapter opens a SECOND L1 actor -> this test FAILS.
# ===========================================================================

def test_relaunch_with_live_l1_does_not_double_spawn(runtime):
    genesis = _genesis()
    fake = FakeAdapter()
    _install_adapter(fake)

    # A LIVE L1 root: recorded running, parent null, and its pane is ALIVE in tmux with a matching uuid.
    live_uuid = "sess-l1-already-live-0001"
    l1 = _binding(
        L1_ADDRESS, level="L1", state="running", generation=6, lease_epoch=3,
        parent_address=None, session_uuid=live_uuid, tmux_target="harness:" + L1_ADDRESS,
    )
    _seed(l1)

    tmux = FakeTmux({"harness:" + L1_ADDRESS: _target_alive(session_uuid=live_uuid)})
    genesis.run_genesis(executor, tmux, _genesis_config(runtime))

    # THE F35 INVARIANT: a live L1 binding is ADOPTED/RESUMED — NO second L1 actor is opened.
    assert _l1_opens(fake) == 0, (
        "F35 resume-not-double-spawn: a live, non-terminal L1 binding must be RESUMED/adopted — "
        "genesis must NOT open a SECOND L1 actor (the daemon is the root custody; one L1 only)"
    )
    # Exactly one non-terminal L1 binding remains (single-owner intact).
    rb = ledger.read_binding(L1_ADDRESS)
    assert rb is not None and not states.is_terminal(rb["state"]), (
        "the live L1 binding must remain non-terminal after an adopt (it was not torn down)"
    )


# ===========================================================================
# RECONCILE_TICK EDGE-TRIGGER (the continuous path, the second Done-test half). Run
# the REAL reconcile_tick repeatedly on a steady-healthy node and assert ZERO WAL
# rows; then flip the detector's liveness to 'dead' ONCE and assert EXACTLY ONE WAL
# row is appended (the single state CHANGE). The continuous path is NOT shipped
# untested. (Driven directly on the REAL reconcile_tick — the daemon's poll body.)
#
# Mutants killed:
#   * append on every poll (level-triggered) -> a steady-healthy poll grows the WAL
#   * never append on a change                -> the one liveness CHANGE yields 0 rows
# ===========================================================================

def test_reconcile_tick_n_steady_healthy_polls_append_zero_rows(runtime):
    _install_adapter(FakeAdapter())
    node = _binding(
        DEAD_LEAF, level="L5", state="running", parent_address=DEAD_LEAF_PARENT,
        session_uuid="sess-steady-0001", tmux_target="harness:" + DEAD_LEAF,
    )
    _seed(node)
    detector = FakeDetector(default="working")  # steady-healthy: the node stays working every poll
    tmux = FakeTmux({"harness:" + DEAD_LEAF: _target_alive(session_uuid="sess-steady-0001")})

    wal_before = len(ledger.load_wal())
    N = 5
    for _ in range(N):
        reconcile.reconcile_tick(executor, tmux, detector)

    assert len(ledger.load_wal()) == wal_before, (
        "EDGE-TRIGGERED: N steady-healthy polls must append ZERO run-ledger rows — only a state/"
        "condition CHANGE appends (DAEMON §5.2). A level-triggered append-every-poll mutant is caught here."
    )


def test_reconcile_tick_one_liveness_change_appends_exactly_one_row(runtime):
    _install_adapter(FakeAdapter())
    node = _binding(
        DEAD_LEAF, level="L5", state="running", parent_address=DEAD_LEAF_PARENT,
        session_uuid="sess-edge-0001", tmux_target="harness:" + DEAD_LEAF,
    )
    _seed(node)
    detector = FakeDetector(default="working")
    tmux = FakeTmux({"harness:" + DEAD_LEAF: _target_alive(session_uuid="sess-edge-0001")})

    # A few steady-healthy polls first (no change -> no rows).
    for _ in range(3):
        reconcile.reconcile_tick(executor, tmux, detector)
    wal_before_change = len(ledger.load_wal())

    # Exactly ONE mid-run liveness CHANGE: the detector now reports the node dead.
    detector.set(DEAD_LEAF, "dead")
    reconcile.reconcile_tick(executor, tmux, detector)
    wal_after_change = len(ledger.load_wal())

    rows_for_change = wal_after_change - wal_before_change
    assert rows_for_change == 1, (
        "EDGE-TRIGGERED: exactly ONE liveness CHANGE (working->dead) must append EXACTLY ONE WAL row "
        f"(the necro death event), got {rows_for_change}. The continuous path appends on the CHANGE, once."
    )
    # The change drove the node terminal (the necro) — the durable fact, not just a WAL row.
    assert ledger.read_binding(DEAD_LEAF)["state"] == "failed", (
        "the liveness change to 'dead' is an owned-but-dead trigger -> the node is necro'd to "
        "'failed' (§3.6 DIED_INFRA; the WAL row)"
    )

    # And a FURTHER steady poll on the now-terminal node appends ZERO more rows (reconcile-once).
    wal_before_settle = len(ledger.load_wal())
    reconcile.reconcile_tick(executor, tmux, detector)
    assert len(ledger.load_wal()) == wal_before_settle, (
        "after the change settles (node terminal), further polls append ZERO rows (reconcile-EXACTLY-once)"
    )


# ===========================================================================
# DAEMON poll_loop — the SINGLE-ITERATION factor. §2.12 names poll_loop(interval_s)
# -> NoReturn (reconcile_tick on a timer), and the interface mandates: "factor it so
# a test can drive a SINGLE iteration." We NEVER run the unbounded loop; we drive
# exactly ONE tick body and assert it ran the REAL reconcile_tick (edge-triggered).
#
# Mutant killed: the loop body is not factored / does not call reconcile_tick -> the
# single-iteration drive does not necro the dead node / does not produce the row.
# ===========================================================================

def test_poll_loop_single_iteration_runs_one_reconcile_tick(runtime):
    daemon = _daemon()
    _install_adapter(FakeAdapter())

    node = _binding(
        DEAD_LEAF, level="L5", state="running", parent_address=DEAD_LEAF_PARENT,
        session_uuid="sess-poll-0001", tmux_target="harness:" + DEAD_LEAF,
    )
    _seed(node)
    detector = FakeDetector(default="dead")  # the node is dead this tick -> the tick necros it
    tmux = FakeTmux({"harness:" + DEAD_LEAF: _target_alive(session_uuid="sess-poll-0001")})

    # Find the single-iteration factor — the loop body that runs ONE reconcile_tick. We require it to
    # exist (the interface mandates it). We NEVER call the unbounded poll_loop.
    one_tick = None
    for name in ("poll_once", "tick_once", "run_one_tick", "_poll_body", "poll_body", "_tick"):
        fn = getattr(daemon, name, None)
        if callable(fn):
            one_tick = fn
            break
    assert one_tick is not None, (
        "daemon must factor the poll_loop body so a test can drive a SINGLE iteration (§2.12: "
        "'factor it so a test can drive a SINGLE iteration'). Expected a poll_once/tick_once/"
        "_poll_body helper running ONE reconcile_tick — none found."
    )

    wal_before = len(ledger.load_wal())
    one_tick(executor, tmux, detector)  # ONE iteration only — never the unbounded loop

    assert ledger.read_binding(DEAD_LEAF)["state"] == "failed", (
        "one poll-loop iteration must run ONE reconcile_tick — the dead node must be necro'd by it "
        "(to 'failed', §3.6 DIED_INFRA)"
    )
    assert len(ledger.load_wal()) == wal_before + 1, (
        "the single tick's ONE liveness change (dead) must append EXACTLY ONE WAL row (edge-triggered)"
    )


# ===========================================================================
# LAUNCHD PLIST — the tracked per-run renderer PROTOTYPE (no daemon is launched).
# Assert crash-only KeepAlive, NO RunAtLoad residency, ThrottleInterval>=10, and
# ProgramArguments naming the fixed request-aware daemon entry.
#
# Mutant killed: KeepAlive=true / RunAtLoad restored -> standing residency returns;
# missing crash-only KeepAlive or a too-small throttle -> a live run is unprotected/thrashes.
# ===========================================================================

def _find_plist():
    """Locate the tracked launchd plist (harnessd/launchd/com.harness.daemon.plist, §2 item 10)."""
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "harnessd" / "launchd" / "com.harness.daemon.plist",
    ]
    # Be permissive about the exact filename, but the directory is pinned by the §3 tree.
    launchd_dir = repo_root / "harnessd" / "launchd"
    if launchd_dir.is_dir():
        candidates.extend(sorted(launchd_dir.glob("*.plist")))
    for p in candidates:
        if p.is_file():
            return p
    return None


def test_launchd_plist_is_wellformed_run_scoped_crash_only_prototype(runtime):
    import plistlib

    plist_path = _find_plist()
    assert plist_path is not None, (
        "the launchd plist must be a TRACKED artifact at harnessd/launchd/com.harness.daemon.plist "
        "(IMPLEMENTATION-PLAN §3 tree, §2 item 10) — not found"
    )
    with plist_path.open("rb") as fh:
        plist = plistlib.load(fh)  # parses only if the plist is well-formed XML

    assert plist.get("Label"), "the launchd plist must carry a Label (the launchd job identity)"

    # Crash-only protection: nonzero/crash restarts; clean idle exit 0 stays down.
    keepalive = plist.get("KeepAlive")
    assert keepalive == {"SuccessfulExit": False}, (
        "the per-run plist must restart crashes only: KeepAlive={SuccessfulExit=false}"
    )

    # No login-resident daemon: harnessctl start bootstraps this prototype on demand.
    assert "RunAtLoad" not in plist, (
        "the run-scoped daemon must not set RunAtLoad; genesis bootstraps it on demand"
    )

    # ThrottleInterval >= 10 (crash-loop guard; DAEMON §7 step 2: ThrottleInterval>=10).
    throttle = plist.get("ThrottleInterval")
    assert isinstance(throttle, int) and throttle >= 10, (
        "the launchd plist must set ThrottleInterval>=10 while a live run is protected — "
        f"got {throttle!r}"
    )

    # ProgramArguments names the daemon entry (the harnessd program), not an empty/garbage argv.
    args = plist.get("ProgramArguments")
    assert isinstance(args, list) and args, "ProgramArguments must be a non-empty list (the daemon argv)"
    blob = " ".join(str(a) for a in args).lower()
    assert "harness" in blob or "daemon" in blob, (
        "ProgramArguments must invoke the harnessd/daemon program (the launchd-hosted daemon entry)"
    )
    assert "--start-request" in args and "--request-id" in args, (
        "fixed launchd argv must carry the one-shot request identity so a crash restart can recover "
        "without re-genesis"
    )


# ===========================================================================
# LOCK-FREE STATUS SIDECAR — the ONE deliberate atomicity carve-out (DAEMON §4.4).
# The daemon writes status.json WITHOUT the EX serialization lock (it is written
# every poll; taking the lock would serialize a non-event against real mutations).
# We prove "lock-free" by POISONING store.file_lock so any attempt to take it
# raises — the status write must STILL succeed (it never takes the lock). And the
# write must be NON-control-state (it appends ZERO WAL rows — the ledger is truth).
#
# Mutant killed: write status under the EX lock -> the poisoned file_lock raises ->
# this test FAILS.
# ===========================================================================

def _status_writer(daemon):
    """Find the daemon's status-sidecar writer (the lock-free §4.4 carve-out)."""
    for name in ("write_status", "write_status_sidecar", "_write_status", "update_status", "status_write"):
        fn = getattr(daemon, name, None)
        if callable(fn):
            return fn
    return None


def test_status_sidecar_written_lock_free(runtime, monkeypatch):
    daemon = _daemon()
    writer = _status_writer(daemon)
    assert writer is not None, (
        "the daemon must expose a status-sidecar writer (write_status/write_status_sidecar) — the "
        "lock-FREE §4.4 carve-out that writes status.json without the EX lock"
    )

    # POISON the EX lock: any attempt to take store.file_lock now raises. A lock-free status write
    # must complete REGARDLESS (it must never enter store.file_lock).
    def _poisoned_lock(*_args, **_kwargs):
        raise AssertionError(
            "the status sidecar must be written LOCK-FREE (DAEMON §4.4) — it must NOT take the EX "
            "serialization lock (that would serialize a non-event against real mutations every poll)"
        )

    monkeypatch.setattr(store, "file_lock", _poisoned_lock)

    wal_before = len(ledger.load_wal())
    # The exact writer signature is the implementer's; we drive it with a permissive status payload.
    try:
        writer(runtime, {"pid": 4242, "started_at": "2026-06-06T00:00:00+00:00", "incarnation": 1})
    except TypeError:
        # A no-arg / runtime-only writer is also acceptable; try the looser shapes.
        try:
            writer(runtime)
        except TypeError:
            writer()

    # The status file landed (best-effort; the ledger is the truth, but the sidecar must still write).
    status_candidates = [
        runtime / ".harnessd" / "status.json",
        runtime / "status.json",
    ]
    found = [p for p in status_candidates if p.is_file()]
    assert found, (
        "the lock-free status write must produce a status.json sidecar (DAEMON §2.3 / §4.4) at "
        ".harnessd/status.json"
    )
    # It is valid JSON (own atomic tmp+rename -> never torn) and carries §2.3 liveness fields.
    data = json.loads(found[0].read_text())
    assert "pid" in data or "started_at" in data or "incarnation" in data, (
        "the status sidecar must carry the §2.3 liveness fields (pid / started_at / incarnation / ...)"
    )

    # It is NOT durable control state: writing status appends ZERO run-ledger rows (the ledger is truth).
    assert len(ledger.load_wal()) == wal_before, (
        "the status sidecar is a best-effort MIRROR, not the durable journal — a status write must "
        "append ZERO WAL rows (DAEMON §4.4: recovery NEVER trusts the sidecar)"
    )


def test_status_sidecar_preserves_runtime_started_at_across_queries(runtime, monkeypatch):
    daemon = _daemon()
    descriptor_started_at = "2026-07-25T06:00:00+00:00"
    (runtime / "runtime.json").write_text(
        json.dumps(
            {
                "build_id": "stable-status-clock",
                "runtime_root": str(runtime),
                "started_at": descriptor_started_at,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    instants = iter(
        [
            "2099-01-01T00:00:00+00:00",
            "2099-01-02T00:00:00+00:00",
        ]
    )
    monkeypatch.setattr(clock, "now_utc", lambda: next(instants))

    first = daemon.write_status(runtime)
    second = daemon.write_status(runtime)

    assert first == second
    payload = json.loads(second.read_text(encoding="utf-8"))
    assert payload["started_at"] == descriptor_started_at
