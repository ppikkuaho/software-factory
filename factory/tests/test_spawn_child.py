"""PARENT-SPAWNS-CHILD bridge — FROZEN acceptance for the live-cascade wiring.
Tests ONLY — NO implementation. RED first.

THE GAP this increment closes. genesis spawns L1; the daemon wires the real adapter + the existing
``harnessctl spawn -> IPC -> chokepoint.claim_and_spawn`` work. But ``claim_and_spawn`` CLAIMS an
EXISTING planned node — it does NOT register one. ``genesis._register_l1_root`` registers L1 then
claims, but parent_address=null (only the L1 root is parentless). There is NO general
parent-registers-briefs-spawns-its-child flow, so the cascade STOPS at L1: a PARENT (e.g. an L2) has
no way to create+brief+spawn its CHILD (e.g. an L3).

THE INCREMENT — add to harnessd/spawn/chokepoint.py::

    register_and_spawn_child(parent_address, child_address, *, child_level_config,
                             brief_content=None, expected_parent_owner_token=None) -> SpawnResult

The ONE path a parent uses to spawn a child (the supervision-tree spawn). STEPS:
  (1) PRECONDITION: the parent_address binding exists + is live/non-terminal (only a LIVE parent
      spawns; a child cannot be spawned under a dead/absent parent). child_address must be under the
      parent subtree.
  (2) REGISTER the child as a fresh planned node (mirror genesis._register_l1_root but parent_address
      SET, not null): generation=0, lease_epoch=1, a minted owner_token, written via the single-writer
      ledger path under the EX lock. Safe if the child already exists planned.
  (3) WRITE THE BRIEF: brief.assemble_neutral writes the child load-manifest into its node; PLUS the
      parent brief_content (the child actual task) is written into the child node workspace (a
      BRIEF.md / work_node).
  (4) SPAWN: chokepoint.claim_and_spawn(child_address, expected_state=planned,
      expected_generation=<registered>, expected_owner_token=<registered token>,
      level_config=child_level_config) — the EXISTING claim-before-spawn (F-024 preserved; on failure
      the claim is released as today).
Plus wire an agent to DRIVE it: harnessctl spawn gains --parent <addr> + --brief <file>; the IPC
_handle_spawn routes to register_and_spawn_child when a parent is given (else the existing claim-only
spawn).

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.11 (the FROZEN chokepoint interface — claim_and_spawn STEP0-5 the child
    spawn reuses), §2.12 (genesis._register_l1_root — the register precedent, parent=null).
  * DAEMON §6.1 (claim-before-spawn STEP0-5, the F-024 fix the child spawn preserves).
  * comms-protocol (only a parent spawns children) + the supervision-tree invariant (every non-root
    node has a declared parent; the L1 root is the ONLY parentless node).

BIAS TO REAL (Lesson 7): we drive the REAL chokepoint.register_and_spawn_child against a REAL on-disk
ledger (tmp RUNTIME_ROOT) with a LIVE PARENT binding seeded; the register + the claim are REAL CAS
writes through the single-writer executor under the REAL EX lock. ONLY the RuntimeAdapter is faked (a
dry-run recorder, exactly Inc 10/12/13). No model, no real pane. This tests the
register+brief+spawn ORCHESTRATION.

NO IMPLEMENTATION here — chokepoint.register_and_spawn_child / the harnessctl --parent/--brief /
the IPC parent-route do not exist yet (RED until written).

LOAD-BEARING (each test names the mutant a wrong impl must fail on):
  * the child is REGISTERED under the parent before the claim (mutant: skip the register -> the claim
    finds no planned node -> the child never reaches running -> caught).
  * parent_address is SET on the child (mutant: leave it null -> the child is a phantom orphan root,
    only L1 is parentless -> caught).
  * the brief is written into the child node (mutant: skip the brief -> the child node has no task /
    no manifest -> caught).
  * a dead/absent-parent spawn is REFUSED (mutant: allow it -> an orphan child spawned under a dead
    parent -> caught; no child binding appears, no actor opened).
  * claim-before-spawn (F-024) preserved (mutant: open the actor before/without a winning claim -> a
    lost child claim must mean pin_and_open was NEVER called -> caught).
  * single-owner: exactly ONE non-terminal binding per child address (mutant: a second register +
    spawn double-opens -> caught).
  * the CLI is NOT a writer: harnessctl spawn --parent --brief routes the parent-spawn THROUGH the
    daemon IPC to register_and_spawn_child (mutant: the CLI writes the ledger itself -> caught).
"""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import socket
import threading
from pathlib import Path

import pytest

import harnessd.config as config
import harnessd.executor as executor  # the REAL single writer the register + claim route through
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.states as states
from harnessd.spawn import chokepoint


# ===========================================================================
# Module-under-construction accessors. The register_and_spawn_child entrypoint
# does not exist yet (RED until written); we resolve it lazily so collection does
# not hard-crash and every test that touches it REDs with a clear AttributeError.
# ===========================================================================

def _register_and_spawn_child():
    """Resolve chokepoint.register_and_spawn_child or FAIL LOUD naming the expected entrypoint."""
    fn = getattr(chokepoint, "register_and_spawn_child", None)
    if not callable(fn):
        raise AssertionError(
            "chokepoint must expose register_and_spawn_child(parent_address, child_address, *, "
            "child_level_config, brief_content=None, expected_parent_owner_token=None) -> SpawnResult "
            "— the ONE parent-spawns-child path (register the child planned under the parent, write "
            "the brief, then claim_and_spawn it; F-024 preserved). It does not exist yet (RED)."
        )
    return fn


def _harnessctl():
    return importlib.import_module("harnessd.harnessctl")


def _ipc():
    """The daemon-side IPC handler home (prefer harnessd.ipc; fall back to harnessd.daemon)."""
    for name in ("harnessd.ipc", "harnessd.daemon"):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if _resolve_handle(mod) is not None and _resolve_serve_one(mod) is not None:
            return mod
    return importlib.import_module("harnessd.ipc")


def _resolve_handle(mod):
    for attr in ("handle_request", "handle", "dispatch", "handle_message"):
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


def _resolve_serve_one(mod):
    for attr in ("serve_one", "accept_one", "handle_one", "serve_once"):
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


def _subcommand_choices(parser) -> set:
    choices: set = set()
    for action in parser._actions:
        if getattr(action, "choices", None):
            try:
                choices.update(action.choices.keys())
            except AttributeError:
                pass
    return choices


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so EVERY pathless REAL
# ledger/executor/chokepoint call (read_binding / append_wal / write_binding /
# the EX lock) lands under the test tree. Restores the prior value (no leak).
# The established suite pattern (test_chokepoint / test_genesis / test_harnessctl).
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
# The FAKE RuntimeAdapter (the ONLY mock — executor + ledger + chokepoint are REAL).
# Records every pin_and_open call so:
#   * the F-024 test can assert len(calls) == 0 on a lost child claim,
#   * the dead/absent-parent test can assert ZERO opens (refused before any spawn),
#   * the single-owner test can count opens precisely.
# A fresh session_uuid per open. Dry-run: no real pane, no model (Inc 9 validated real).
# ===========================================================================

def _spawn_result_cls():
    base = importlib.import_module("harnessd.spawn.adapters.base")
    return base.SpawnResult


class FakeAdapter:
    """Records pin_and_open; returns a happy dry-run SpawnResult carrying the spawn facts."""

    def __init__(self):
        self.calls = []
        self._n = 0

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        self._n += 1
        SpawnResult = _spawn_result_cls()
        return SpawnResult(
            ok=True,
            session_uuid=f"sess-child-spawned-{self._n:04d}",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L3"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path=f"/runtime/transcripts/sess-child-spawned-{self._n:04d}.jsonl",
            failure_class=None,
        )


def _install_adapter(fake):
    """Inject the FAKE adapter into the REAL chokepoint via its module-level seam (set_adapter/ADAPTER).

    The increment carries NO adapter param (the §2.11 precedent) — the adapter is injected like
    ledger.RUNTIME_ROOT. register_and_spawn_child must spawn THROUGH this same real chokepoint, so the
    child open is recorded on this fake.
    """
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
        return
    if hasattr(chokepoint, "ADAPTER"):
        chokepoint.ADAPTER = fake
        return
    raise AssertionError(
        "chokepoint exposes no adapter-injection seam: expected a module-level ``ADAPTER`` attribute "
        "or a ``set_adapter(adapter)`` setter (the frozen signature carries no adapter param)."
    )


# ===========================================================================
# Seeding — write bindings DIRECTLY through the REAL ledger (the suite-wide
# lock-held seeding path: ledger.write_binding(map, _lock_held=True)). ADDITIVE
# (read the whole map, splice in) so seeding two nodes does not clobber the first.
# ===========================================================================

PARENT = "proj/feature#exec"          # a LIVE L2 parent
CHILD = "proj/feature/widget#exec"    # an L3 child UNDER the parent subtree
PARENT_SUBAGENT = "subagent-parent-1"
PARENT_SESSION = "sess-uuid-parent-0001"


def _live_parent_binding(
    *,
    node_address=PARENT,
    parent_address="proj#exec",
    state="running",
    level="L2",
    generation=4,
    lease_epoch=2,
    subagent_id=PARENT_SUBAGENT,
    session_uuid=PARENT_SESSION,
):
    """A LIVE (non-terminal) parent binding + its owner_token."""
    token = fencing.mint_owner_token(node_address, subagent_id, session_uuid, lease_epoch)
    binding = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "tmux_target": "harness:" + node_address,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "terminal_signal": None,
        "gate_crossed_at": None,
        "paused_at": None,
    }
    return binding, token


def _seed(*bindings):
    """ADDITIVE seed: splice each binding into the live whole map (no bystander clobber)."""
    whole_map = ledger.all_nodes()
    for binding in bindings:
        whole_map[binding["node_address"]] = copy.deepcopy(binding)
    ledger.write_binding(whole_map, _lock_held=True)


def _read(node):
    return ledger.read_binding(node)


def _child_level_config():
    return config.LevelConfig.for_level("L3")


def _ok(result) -> bool:
    return bool(getattr(result, "ok", False))


def _non_terminal_count(node_address: str) -> int:
    """Count NON-terminal bindings at ``node_address`` in the keyed map (single-owner check).

    The keyed binding map holds at most one slice per address, so the supervision-tree single-owner
    invariant (exactly ONE non-terminal binding per child address) reads as: the address is present
    AND its state is non-terminal -> exactly 1; absent or terminal -> 0.
    """
    binding = ledger.read_binding(node_address)
    if binding is None:
        return 0
    return 0 if states.is_terminal(binding.get("state")) else 1


def _child_brief_blob(child_address: str, runtime_root: Path) -> str:
    """Return everything observable about the child's written brief, concatenated for scanning.

    The brief lands SOMEWHERE the child can read it (DAEMON §6.1 STEP2 + the increment: the assembled
    manifest + the parent brief_content into the child node workspace — a BRIEF.md / work_node). We do
    NOT pin the exact path (FORK-BRIEF-LANDING is the builder's): we scan BOTH (a) the binding slice
    (a recorded brief / workspace / brief_content field) AND (b) any on-disk file under the runtime
    tree that mentions the child address. The load-bearing fact is that the brief content is RECOVERABLE
    from the child node, not its precise home.
    """
    parts: list[str] = []
    binding = ledger.read_binding(child_address) or {}
    parts.append(json.dumps(binding, sort_keys=True, default=str))
    root = Path(runtime_root)
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    parts.append(path.read_text(encoding="utf-8", errors="ignore"))
                except (OSError, UnicodeError):
                    continue
    return "\n".join(parts)


# ===========================================================================
# (a) THE FULL ARC. A live PARENT spawns a CHILD under its subtree: the child node
#     appears REGISTERED planned THEN claimed->spawning->running, parent_address SET
#     to the parent, generation/owner_token coherent.
#
#     Mutants killed:
#       * skip the register -> claim_and_spawn finds no planned node -> child never runs.
#       * parent_address null -> the child is an orphan root (only L1 is parentless).
#       * actor opened before the claim -> the F-024 ordering breaks (asserted positively).
# ===========================================================================

def test_parent_spawns_child_runs_the_full_arc(runtime):
    fake = FakeAdapter()
    _install_adapter(fake)

    parent, parent_token = _live_parent_binding(state="running")
    _seed(parent)
    assert _read(CHILD) is None, "precondition: the child does not exist before the parent spawns it"

    # Record the child binding STATE the adapter sees at pin_and_open time. The child must ALREADY be
    # registered AND claimed (state == 'claimed') when the actor opens — register-then-claim-before-spawn.
    seen = []
    original_pin = fake.pin_and_open

    def recording_pin(neutral_brief, level_config, tmux_target, env):
        live = ledger.read_binding(CHILD)
        seen.append(None if live is None else (live.get("state"), live.get("parent_address")))
        return original_pin(neutral_brief, level_config, tmux_target, env)

    fake.pin_and_open = recording_pin

    register_and_spawn_child = _register_and_spawn_child()
    result = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content="Implement the widget per the frozen acceptance.",
        expected_parent_owner_token=parent_token,
    )

    assert _ok(result) is True, "a live parent spawning a child under its subtree must succeed"
    assert len(fake.calls) == 1, "the child actor must be opened EXACTLY once (one register+claim+spawn)"

    # The child was REGISTERED (it now exists) and is RUNNING (the full claim->spawning->running arc).
    child = _read(CHILD)
    assert child is not None, (
        "the child node must be REGISTERED (a planned slot created under the parent) — skipping the "
        "register leaves claim_and_spawn with no planned node to claim (mutant caught)"
    )
    assert child["state"] == "running", (
        "the child must run the FULL arc registered-planned -> claimed -> spawning -> running "
        "(mutant: skip the register / never claim -> the child never reaches running)"
    )

    # parent_address is SET to the parent (the supervision-tree edge — only L1 is parentless).
    assert child.get("parent_address") == PARENT, (
        "the child must declare parent_address == the spawning parent (mutant: null parent -> the "
        "child is a phantom orphan root; only the L1 root is parentless — DAEMON §7)"
    )

    # F-024 (positive): at pin_and_open time the child was already CLAIMED under the parent.
    assert seen and seen[0] is not None, "the adapter must have been reached (pin_and_open called)"
    seen_state, seen_parent = seen[0]
    assert seen_state == "claimed", (
        "claim-before-spawn: when the child actor opens, the child slot must ALREADY be ``claimed`` "
        f"(saw {seen_state!r}) — the register+claim is STRICTLY before the actor"
    )
    assert seen_parent == PARENT, "the claimed child must already carry parent_address == the parent"

    # generation / owner_token are coherent (a real CAS-driven arc: planned gen0 -> ... -> running).
    assert isinstance(child.get("generation"), int) and child["generation"] >= 1, (
        "the child generation must have advanced through the real claim/spawn transitions (>=1)"
    )
    assert child.get("owner_token"), "the running child must carry a non-empty owner_token"


# ===========================================================================
# (b) THE CHILD BRIEF. The parent brief_content + the assembled load-manifest are
#     present in the child node (a BRIEF.md / work_node).
#
#     Mutant killed: skip writing the brief -> the child node has no task / no manifest -> caught.
# ===========================================================================

def test_child_brief_carries_parent_content_and_assembled_manifest(runtime):
    fake = FakeAdapter()
    _install_adapter(fake)

    parent, parent_token = _live_parent_binding(state="running")
    _seed(parent)

    sentinel = "BRIEF-SENTINEL-implement-the-frobnicator-per-acceptance"
    register_and_spawn_child = _register_and_spawn_child()
    result = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content=sentinel,
        expected_parent_owner_token=parent_token,
    )
    assert _ok(result) is True

    blob = _child_brief_blob(CHILD, runtime)

    # (1) The PARENT brief_content (the child's actual task) landed in the child node.
    assert sentinel in blob, (
        "the parent brief_content (the child's actual task) must be written into the child node "
        "(a BRIEF.md / work_node) — mutant: skip writing the brief -> the child has no task -> caught"
    )

    # (2) The ASSEMBLED load-manifest (the role-as-documents the child reads in place) landed too.
    # The L3 manifest names the per-level role docs (operational/L3/{soul,role,config}.md) — at least
    # one manifest path must be recoverable from the child node (the brief carries the manifest).
    assert ("operational/L3/role.md" in blob) or ("operational/L3/soul.md" in blob), (
        "the assembled load-manifest (brief.assemble_neutral) must be written into the child node — "
        "the child reads its role docs in place (role-as-documents); mutant: skip the manifest -> caught"
    )


# ===========================================================================
# (c) DEAD / ABSENT PARENT IS REFUSED. A child cannot be spawned under a dead or
#     absent parent — no orphan child (only a LIVE parent spawns).
#
#     Mutant killed: allow the spawn -> an orphan child node appears / an actor opens -> caught.
# ===========================================================================

def test_spawn_under_absent_parent_is_refused(runtime):
    fake = FakeAdapter()
    _install_adapter(fake)
    # NO parent seeded — the parent address is ABSENT from the ledger.
    assert _read(PARENT) is None

    register_and_spawn_child = _register_and_spawn_child()
    result = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content="task under a non-existent parent",
        expected_parent_owner_token="any-token",
    )

    assert _ok(result) is False, (
        "a spawn under an ABSENT parent must be REFUSED (only a LIVE parent spawns a child) — "
        "mutant: allow it -> an orphan child under a non-existent parent"
    )
    assert _read(CHILD) is None, (
        "a refused spawn must register NO child node — no orphan binding is created under an absent parent"
    )
    assert len(fake.calls) == 0, (
        "a refused spawn must open NO actor (the refusal is BEFORE the register/claim/adapter)"
    )


def test_spawn_under_dead_parent_is_refused(runtime):
    fake = FakeAdapter()
    _install_adapter(fake)

    # The parent exists but is in a TERMINAL state (dead) — a dead parent cannot spawn a child.
    dead_parent, parent_token = _live_parent_binding(state="dead")
    _seed(dead_parent)
    assert states.is_terminal(_read(PARENT)["state"]), "precondition: the parent is terminal (dead)"

    register_and_spawn_child = _register_and_spawn_child()
    result = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content="task under a dead parent",
        expected_parent_owner_token=parent_token,
    )

    assert _ok(result) is False, (
        "a spawn under a DEAD (terminal) parent must be REFUSED — only a LIVE/non-terminal parent "
        "spawns a child (mutant: allow it -> an orphan child under a dead parent)"
    )
    assert _read(CHILD) is None, (
        "a refused dead-parent spawn must register NO child node (no orphan binding)"
    )
    assert len(fake.calls) == 0, "a refused dead-parent spawn must open NO actor"


# ===========================================================================
# (d) THE SUPERVISION-TREE INVARIANT. The child declares parent_address; exactly ONE
#     non-terminal binding exists per child address (single-owner).
#
#     Mutants killed:
#       * null parent_address -> the child violates the every-non-root-has-a-parent invariant.
#       * a second register+spawn double-opens -> more than one non-terminal binding / two actors.
# ===========================================================================

def test_child_declares_parent_address_single_owner(runtime):
    fake = FakeAdapter()
    _install_adapter(fake)

    parent, parent_token = _live_parent_binding(state="running")
    _seed(parent)

    register_and_spawn_child = _register_and_spawn_child()
    result = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content="the one child task",
        expected_parent_owner_token=parent_token,
    )
    assert _ok(result) is True

    child = _read(CHILD)
    assert child is not None and child.get("parent_address") == PARENT, (
        "the supervision-tree invariant: every non-root node DECLARES its parent_address "
        "(the child names the spawning parent)"
    )
    assert _non_terminal_count(CHILD) == 1, (
        "exactly ONE non-terminal binding must exist per child address (single-owner)"
    )

    # A SECOND parent-spawn of the SAME child (the child is now running) must NOT double-open a second
    # live actor: the existing claim-before-spawn (F-024) means the registered-planned re-register +
    # claim cannot win against a RUNNING child via the planned-expected claim, so no second open lands.
    second = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content="a racing duplicate child task",
        expected_parent_owner_token=parent_token,
    )

    # Whatever the result shape, the structural invariant must hold: still exactly ONE non-terminal
    # binding for the child, and NOT two opened actors (no double-spawn of a live child).
    assert _non_terminal_count(CHILD) == 1, (
        "a second parent-spawn of an already-running child must NOT create a second non-terminal child "
        "binding (single-owner; mutant: re-register-and-open -> a double-spawned live child -> caught)"
    )
    assert len(fake.calls) <= 1, (
        "a second parent-spawn of a RUNNING child must NOT open a SECOND actor (claim-before-spawn / "
        "no double-spawn of a live address — F35/F-024 preserved across the parent-spawn path)"
    )
    assert _ok(second) is False, (
        "the racing duplicate spawn of a live child must NOT report a fresh successful spawn "
        "(the live child is already running; re-claiming a running slot as planned loses)"
    )


# ===========================================================================
# (e) F-024 PRESERVED. If the child CLAIM is lost, NO actor is opened.
#
#     We make the underlying claim_and_spawn LOSE by registering the child ourselves at a generation
#     the parent-spawn's claim will not match: we wrap executor.claim so the FIRST claim of the child
#     address aborts (a stale CAS precondition), exactly as a concurrent/stale session would. The
#     load-bearing fact: a LOST child claim means adapter.pin_and_open was NEVER called.
#
#     Mutant killed: open the actor before/without a winning claim -> pin_and_open called on a lost
#     claim -> caught.
# ===========================================================================

def test_f024_lost_child_claim_opens_no_actor(runtime, monkeypatch):
    fake = FakeAdapter()
    _install_adapter(fake)

    parent, parent_token = _live_parent_binding(state="running")
    _seed(parent)

    # Force the child's claim to LOSE: intercept executor.claim and, for the CHILD address, return a
    # not-ok TransitionResult (a CAS miss) WITHOUT opening the actor — exactly the F-024 lost-claim path.
    real_claim = executor.claim
    TransitionResult = executor.TransitionResult

    def losing_claim(node_address, **kwargs):
        if node_address == CHILD:
            live = ledger.read_binding(node_address)
            return TransitionResult(
                ok=False,
                errors=["forced CAS miss (test): the child claim is lost (concurrent/stale claim)"],
                warnings=[],
                binding=live,
            )
        return real_claim(node_address, **kwargs)

    # Patch the claim the chokepoint actually calls (it imports executor and calls executor.claim).
    monkeypatch.setattr(executor, "claim", losing_claim)

    register_and_spawn_child = _register_and_spawn_child()
    result = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content="task whose child claim is lost to a racer",
        expected_parent_owner_token=parent_token,
    )

    assert _ok(result) is False, "a lost child claim must NOT report a successful spawn (ClaimLost)"
    assert len(fake.calls) == 0, (
        "F-024: a LOST child claim must mean adapter.pin_and_open was NEVER called — the child slot is "
        "claimed in control-plane state BEFORE the actor opens (claim STRICTLY before actor); mutant: "
        "open the actor before/without a winning claim -> pin_and_open called on a lost claim -> caught"
    )


# ===========================================================================
# Non-orphan-precondition strengthen: the refusal of a non-live parent is DECIDED
# BEFORE the child is registered — proven by the child remaining absent (no half-
# registered orphan slot a later sweep would adopt). This pins "PRECONDITION first".
# ===========================================================================

def test_dead_parent_refusal_leaves_no_half_registered_child(runtime):
    fake = FakeAdapter()
    _install_adapter(fake)

    dead_parent, parent_token = _live_parent_binding(state="failed")  # terminal (failed)
    _seed(dead_parent)

    register_and_spawn_child = _register_and_spawn_child()
    result = register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=_child_level_config(),
        brief_content="task under a failed parent",
        expected_parent_owner_token=parent_token,
    )

    assert _ok(result) is False
    # The precondition fires BEFORE the register: no planned (or any) child slot is left behind.
    assert _read(CHILD) is None, (
        "the live-parent PRECONDITION must be checked BEFORE the child register — a refused spawn "
        "leaves NO half-registered child slot (mutant: register first, then check -> an orphan planned "
        "child a later reconcile sweep could adopt -> caught)"
    )
    assert _non_terminal_count(CHILD) == 0


# ===========================================================================
# (f) THE CLI IS NOT A WRITER. harnessctl spawn --parent <addr> --brief <file>
#     routes the parent-spawn THROUGH the daemon IPC to register_and_spawn_child;
#     the CLI client never writes the ledger. Driven over a REAL unix socket with a
#     BOUNDED single-accept handler (the suite-wide harness pattern).
#
#     Mutants killed:
#       * the CLI writes the ledger directly -> the source/route assertions catch it.
#       * the IPC ignores --parent and falls to the claim-only spawn -> the child is never
#         registered under the parent -> the round-trip does not produce a running child.
# ===========================================================================

def _recv_all(conn) -> bytes:
    chunks = []
    while True:
        data = conn.recv(65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


class BoundedDaemonIPC:
    """A real AF_UNIX socket bound to a bounded single-accept handler thread (suite-wide pattern).

    The handler thread runs the REAL daemon IPC handler ONCE per accepted connection for a fixed,
    finite number of connections, then exits. No serve-forever loop; the fixture joins it.
    """

    def __init__(self, socket_path: Path, *, expected_connections: int = 1):
        self.socket_path = Path(socket_path)
        self.expected = expected_connections
        self.handled: list[dict] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.socket_path))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._run, name="bounded-ipc-child", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        handle = _resolve_handle(_ipc())
        for _ in range(self.expected):  # BOUNDED — not serve-forever
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return
            with conn:
                raw = _recv_all(conn)
                request = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                self.handled.append(request)
                response = handle(request)
                conn.sendall(json.dumps(response).encode("utf-8"))

    def close(self):
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture
def ipc_server(runtime, tmp_path):
    socket_path = tmp_path / "harnessd-child.sock"
    server = BoundedDaemonIPC(socket_path, expected_connections=1).start()
    try:
        yield server
    finally:
        server.close()


def _run_cli(argv, *, socket_path=None, capsys=None, monkeypatch=None):
    """Run harnessctl.main(argv) and return (exit_code, parsed_json_stdout_or_None).

    Threads socket_path by the FIRST channel main() accepts: a socket_path/sock kwarg, else a --socket
    flag, else the HARNESSD_SOCKET env var (does not over-constrain the wiring detail).
    """
    harnessctl = _harnessctl()
    main = harnessctl.main

    full_argv = list(argv)
    kwargs = {}
    if socket_path is not None:
        params = inspect.signature(main).parameters
        if "socket_path" in params:
            kwargs["socket_path"] = str(socket_path)
        elif "sock" in params:
            kwargs["sock"] = str(socket_path)
        else:
            full_argv = ["--socket", str(socket_path)] + full_argv
            if monkeypatch is not None:
                monkeypatch.setenv("HARNESSD_SOCKET", str(socket_path))

    code = main(full_argv, **kwargs)

    payload = None
    if capsys is not None:
        out = capsys.readouterr().out
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    return code, payload


def test_harnessctl_spawn_parent_brief_routes_through_the_daemon(ipc_server, runtime, capsys, monkeypatch):
    """harnessctl spawn --parent <addr> --brief <file> -> IPC -> register_and_spawn_child (real socket).

    The CLI is a CLIENT: it serializes a request carrying the parent address + the brief and ships it
    over the real unix socket; the DAEMON performs the parent-spawn through register_and_spawn_child
    inside the one lock. After the round-trip the CHILD is registered under the parent AND running.
    """
    _install_adapter(FakeAdapter())  # the spawn adapter is faked; the register+claim are REAL

    parent, parent_token = _live_parent_binding(state="running")
    _seed(parent)
    assert _read(CHILD) is None, "precondition: the child does not exist before the CLI parent-spawn"

    # Write the brief to a file the --brief flag points at (the child's actual task).
    brief_sentinel = "CLI-ROUTED-BRIEF-build-the-thing-per-acceptance"
    brief_file = Path(runtime) / "child-brief.md"
    brief_file.write_text(brief_sentinel, encoding="utf-8")

    # The parser must accept --parent and --brief on the spawn subcommand (else parse_args errors here).
    parser = _harnessctl().build_parser()
    spawn_choices = _subcommand_choices(parser)
    assert "spawn" in spawn_choices, "the spawn subcommand must exist"

    code, payload = _run_cli(
        [
            "spawn", CHILD,
            "--parent", PARENT,
            "--brief", str(brief_file),
            "--level", "L3",
            "--expected-owner-token", parent_token,  # the parent's token (the live-parent precondition)
        ],
        socket_path=ipc_server.socket_path,
        capsys=capsys,
        monkeypatch=monkeypatch,
    )

    # The handler received the request over the real socket (the round-trip happened) and it carried
    # the parent address (the route discriminator the IPC uses to pick register_and_spawn_child).
    assert ipc_server.handled, (
        "the spawn --parent request must round-trip through the daemon over the real socket "
        "(the CLI is a client; the daemon performs the parent-spawn)"
    )
    routed = ipc_server.handled[0]
    assert routed.get("parent") == PARENT or routed.get("parent_address") == PARENT, (
        "the CLI must serialize the --parent address into the request so the IPC can ROUTE to "
        "register_and_spawn_child (mutant: drop --parent -> the IPC falls to the claim-only spawn -> "
        "the child is never registered under the parent)"
    )

    # AFTER the round-trip the ledger reflects the parent-spawn performed BY THE DAEMON: the child is
    # registered UNDER THE PARENT and running (the IPC routed to register_and_spawn_child, not the
    # claim-only spawn that would have found no planned child to claim).
    child = _read(CHILD)
    assert child is not None, (
        "after the round-trip the child must be REGISTERED by the daemon (the IPC routed --parent to "
        "register_and_spawn_child) — mutant: the IPC ignores --parent -> no child registered -> caught"
    )
    assert child.get("parent_address") == PARENT, "the daemon-registered child must declare its parent"
    assert child["state"] == "running", (
        "the daemon must run the full register->claim->spawn arc for the child (state == running)"
    )

    # The brief content the CLI shipped landed in the child node (the parent-spawn wrote the brief).
    blob = _child_brief_blob(CHILD, runtime)
    assert brief_sentinel in blob, (
        "the brief the CLI shipped (--brief <file>) must be written into the child node by the daemon "
        "(the parent brief_content -> the child's actual task)"
    )


def test_harnessctl_client_module_is_not_a_writer():
    """The harnessctl CLIENT module never imports/calls the single-writer primitives (cardinal rule).

    The parent-spawn (register_and_spawn_child) is a WRITE — it MUST be performed by the daemon, never
    by the CLI process. So the harnessctl module's source must not call register_and_spawn_child /
    claim_and_spawn / executor writers directly (mutant: have the CLI write the ledger -> caught).
    """
    harnessctl = _harnessctl()
    source = inspect.getsource(harnessctl)
    for writer in (
        "register_and_spawn_child",
        "claim_and_spawn",
        "executor.transition",
        "executor.claim",
        "ledger.write_binding",
    ):
        assert writer not in source, (
            f"harnessctl (the CLIENT) must NOT call {writer!r} — the parent-spawn is performed BY THE "
            "DAEMON inside the one lock (DAEMON §4.3: CLIs are clients, not writers)"
        )
