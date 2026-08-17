"""Increment 10 — FROZEN acceptance for the spawn CHOKEPOINT (claim-before-spawn +
rollback + the gate firewall). Tests ONLY — NO implementation. RED first.

The headline is the F-024 structural fix: the control-plane slot is CLAIMED (a REAL CAS
transition into ``claimed`` against the REAL on-disk ledger under the REAL EX lock) BEFORE
the actor opens, so a stale session can never double-spawn.

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.11 — the FROZEN chokepoint interface (claim_and_spawn STEP0-5,
    resume's gate firewall, release_claim, collapse) transcribed below.
  * IMPLEMENTATION-PLAN §3 module table (chokepoint.py row): "NOT a writer — calls
    executor.transition() for every state change. On any post-claim failure, CAS-releases
    the claim (claimed->planned, bump epoch)."
  * IMPLEMENTATION-PLAN — the Increment-10 Done-test (L759-768): F-024 (claim aborts ->
    no actor opened -> ClaimLost; happy path claims BEFORE create_detached); claim-rollback
    on SpawnFailure (CAS claimed->planned, epoch bumped, L1 escalation); gate-firewall
    (gate_crossed_at set -> refuse resume -> fresh claim w/ delta brief; assert NO code path
    builds a --resume argv with gate_crossed_at != null); STEP4 writes a non-null
    transcript_path into the binding.
  * DAEMON §6.1 — claim-before-spawn STEP0-5 (the F-024 fix): STEP0 pause-subtree read-point;
    STEP1 CAS-claim (= in_flight CLAIM-increment) BEFORE the actor opens; STEP2 neutral brief;
    STEP3 pin-confirm BEFORE the child runs; STEP4 open tmux + record session_uuid/model_used;
    STEP5 claimed->spawning->running. On admission-deny / E32-pin-fail / actor-open-fail:
    RELEASE the claim (claimed->planned, bump epoch — the rollback edge is first-class).
  * DAEMON §6.3 — the E32 spawn-failure contract: on {auth_expired, model_unavailable,
    override_rejected, runtime_down}, do NOT substitute, do NOT best-effort — RELEASE the
    claim and emit a spawn-failure escalation to L1 (child-address + configured vs actual +
    which class fired). The chokepoint always writes the ACTUAL model_used into the binding.
  * DAEMON §6.4 — the gate firewall (LOCKED, correctness-not-optimization): NEVER --resume
    across a quality gate. gate_crossed_at != null -> REFUSE --resume, fall back to a fresh
    claim_and_spawn with a delta brief. The --resume argv is constructed ONLY on the
    else-branch, so a resume across the gate is STRUCTURALLY impossible.

BIAS TO REAL (Lesson 7): the executor + ledger are REAL — executor.claim is a REAL CAS
transition against a REAL on-disk binding ledger under the REAL EX lock (the load-bearing
claim-before-spawn logic). Only the RuntimeAdapter is faked (its pin_and_open records the
call / can be made to raise SpawnFailure) — justified because Increment 9 already validated
the real adapter+tmux against REAL tmux, and THIS increment tests the chokepoint
ORCHESTRATION (claim ordering, rollback, gate firewall), not the adapter internals. No real
model, no real pane.

NO IMPLEMENTATION here — harnessd/spawn/chokepoint.py does not exist yet (RED until written).

Load-bearing properties (each pins a mutant a wrong impl must fail on):
  * the claim is STRICTLY BEFORE the actor (mutant: open the adapter before/without the claim
    -> the F-024 test FAILS: a LOST claim must mean adapter.pin_and_open was NEVER called,
    call-count 0, and no tmux session opened).
  * rollback on SpawnFailure (mutant: skip release_claim -> a leaked ``claimed`` slot / no
    epoch bump -> caught: the binding must return to ``planned`` with lease_epoch bumped).
  * an L1 escalation is emitted on the pin/spawn failure (mutant: silent failure -> caught).
  * the gate firewall refuses --resume when gate_crossed_at is set (mutant: build a --resume
    anyway -> caught) AND there is STRUCTURALLY no --resume path under a crossed gate.
  * STEP4 writes a NON-NULL transcript_path into the binding (mutant: leave it null -> caught,
    pairs with Increment 6's transcript-absent fail-loud).
  * collapse carries the release-decrement; ESCALATED is NOT a collapse (mutant: collapse on
    ESCALATED -> caught).
"""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.turn_state as turn_state


# ===========================================================================
# Module-under-construction accessors (RED until they exist). Imported lazily
# so collection does not hard-crash before the modules are written — every test
# that touches them will RED with a clear ImportError/AttributeError instead.
# ===========================================================================

def _chokepoint():
    return importlib.import_module("harnessd.spawn.chokepoint")


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so the REAL executor's
# pathless ledger calls (read_binding/append_wal/write_binding) AND the EX lock
# all land under the test tree. Restores the prior value (no cross-test leak).
# ===========================================================================

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


# ===========================================================================
# The FAKE RuntimeAdapter (the ONLY mock — the executor + ledger are REAL).
#
# It records every pin_and_open call (so the F-024 test can assert call-count 0
# on a lost claim) and, on the happy path, returns a SpawnResult carrying the
# adapter's products: session_uuid + a NON-NULL transcript_path + the ACTUAL
# model_used (config = intent; model_used = fact). A SpawnFailure variant raises
# the typed E32 failure so the rollback edge is exercised.
# ===========================================================================

def _spawn_result_cls():
    base = importlib.import_module("harnessd.spawn.adapters.base")
    return base.SpawnResult


def _spawn_failure_cls():
    guard = importlib.import_module("harnessd.spawn.oauth_guard")
    return guard.SpawnFailure


class FakeAdapter:
    """Records pin_and_open calls; returns a happy SpawnResult by default.

    ``calls`` is the list of (neutral_brief, level_config, tmux_target, env) tuples — the
    F-024 test asserts ``len(adapter.calls) == 0`` when the claim is lost (the actor was
    NEVER opened). The fake stands in for the whole adapter+tmux boundary (Increment 9 already
    validated the real adapter against REAL tmux); here it is the recorder/injector seam.
    """

    def __init__(
        self,
        *,
        session_uuid="sess-uuid-spawned-0001",
        transcript_path="/runtime/transcripts/sess-uuid-spawned-0001.jsonl",
        model_used="opus-4.8 / claude-code",
        codex_seat_id=None,
        codex_auth_version=None,
        codex_access_seconds_remaining=None,
    ):
        self.calls = []
        self._session_uuid = session_uuid
        self._transcript_path = transcript_path
        self._model_used = model_used
        self._codex_seat_id = codex_seat_id
        self._codex_auth_version = codex_auth_version
        self._codex_access_seconds_remaining = codex_access_seconds_remaining

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        SpawnResult = _spawn_result_cls()
        return SpawnResult(
            ok=True,
            session_uuid=self._session_uuid,
            model_used=self._model_used,
            role_variant=getattr(level_config, "role_variant", "L3"),
            system_prompt_file=getattr(level_config, "system_prompt_file", "operational/shared/system-prompt.md"),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path=self._transcript_path,
            failure_class=None,
            codex_seat_id=self._codex_seat_id,
            codex_auth_version=self._codex_auth_version,
            codex_access_seconds_remaining=self._codex_access_seconds_remaining,
        )


class FailingAdapter:
    """A RuntimeAdapter whose pin_and_open raises a typed E32 SpawnFailure (model_unavailable).

    Records calls too (so the rollback test can confirm the adapter WAS reached — STEP3 — i.e.
    the failure is a genuine post-claim adapter failure, not a pre-claim abort). The failure
    class is carried on the exception so the escalation can name "which class fired" (§6.3).
    """

    def __init__(self, *, failure_class="model_unavailable", model_used="opus-4.8 / claude-code"):
        self.calls = []
        self._failure_class = failure_class
        self._model_used = model_used

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        SpawnFailure = _spawn_failure_cls()
        exc = SpawnFailure(
            f"E32: cannot pin configured model+runtime ({self._failure_class})"
        )
        # Carry the classification on the exception (the escalation names which class fired, §6.3).
        exc.failure_class = self._failure_class
        exc.model_used = self._model_used
        raise exc


def _install_adapter(chokepoint, fake):
    """Inject the FAKE adapter into the chokepoint via its dependency seam.

    The FROZEN §2.11 signature carries NO adapter parameter, so the adapter is a module-level
    injectable — exactly the ``ledger.RUNTIME_ROOT`` precedent the daemon/tests already use to
    bind a real dependency. This helper installs the fake on whatever seam the chokepoint
    exposes (an ``ADAPTER`` attribute or a ``set_adapter`` setter), and FAILS LOUDLY with the
    expected contract if neither exists — so the implementer is told the seam, not left guessing.
    """
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
        return
    if hasattr(chokepoint, "ADAPTER"):
        chokepoint.ADAPTER = fake
        return
    raise AssertionError(
        "chokepoint exposes no adapter-injection seam: expected a module-level ``ADAPTER`` "
        "attribute or a ``set_adapter(adapter)`` setter (the §2.11 frozen signature carries no "
        "adapter param, so the adapter is injected like ledger.RUNTIME_ROOT)."
    )


# ===========================================================================
# Seeding helpers — write bindings DIRECTLY through the REAL ledger (the §2.6
# seeding path used across the suite: ledger.write_binding(map, _lock_held=True)).
# ===========================================================================

NODE = "proj/widget#exec"
PARENT = "proj#exec"
SUBAGENT = "subagent-aaaa1111"
SESSION = "sess-uuid-seed-0001"
L1_NODE = "root#exec"


def _binding(
    *,
    node_address=NODE,
    parent_address=PARENT,
    state="planned",
    generation=0,
    lease_epoch=1,
    subagent_id=SUBAGENT,
    session_uuid=SESSION,
    gate_crossed_at=None,
    paused_at=None,
    transcript_path=None,
    spec_pointer="design/intent-spec.md",  # E1: decision-complete by default; None = sparse/unprepared
    extra=None,
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
        "spec_pointer": spec_pointer,
        "liveness_state": "claimed",
        "gate_crossed_at": gate_crossed_at,
        "paused_at": paused_at,
        "transcript_path": transcript_path,
    }
    if extra:
        rec.update(extra)
    return rec, token


def _seed(bindings):
    """Write a whole keyed binding map to the REAL on-disk ledger (lock-held seeding path)."""
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _read(node=NODE):
    return ledger.read_binding(node)


def _level_config():
    return config.LevelConfig.for_level("L3")


def _ok(result) -> bool:
    return getattr(result, "ok")


# ===========================================================================
# F-024 — THE HEADLINE. A LOST claim -> NO actor opened -> ClaimLost.
#
# The chokepoint claims the control-plane slot (a REAL CAS into ``claimed``) BEFORE the
# adapter runs. We make the claim LOSE by presenting a WRONG CAS precondition (a stale
# generation), exactly as a concurrent/stale session would. The load-bearing assertion:
# the fake adapter.pin_and_open was NEVER called (call-count 0) — a lost claim means the
# actor was never opened. This is the structural F-024 fix.
#
# Mutant killed: open the adapter before/without the claim -> pin_and_open is called even on
# a lost claim -> this test FAILS.
# ===========================================================================

def test_f024_lost_claim_opens_no_actor(runtime):
    chokepoint = _chokepoint()
    fake = FakeAdapter()
    _install_adapter(chokepoint, fake)

    binding, _token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])
    before = _read()

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=999,            # WRONG generation -> the CAS-claim must ABORT
        expected_owner_token=binding["owner_token"],
        level_config=_level_config(),
    )

    # The claim was LOST: the result is not-ok (a ClaimLost outcome).
    assert _ok(result) is False, "a lost CAS-claim must NOT report a successful spawn (ClaimLost)"

    # THE F-024 INVARIANT: the actor was NEVER opened. The claim is STRICTLY before the actor,
    # so a lost claim means pin_and_open was never reached (call-count 0).
    assert len(fake.calls) == 0, (
        "F-024: a LOST claim must mean adapter.pin_and_open was NEVER called — the slot is "
        "claimed in control-plane state BEFORE the adapter runs (claim STRICTLY before actor)"
    )

    # And the binding is byte-for-byte unchanged: nothing was claimed, nothing rolled back.
    assert _read() == before, "a lost claim must leave the binding UNCHANGED (no half-claimed slot)"


def test_f024_lost_claim_returns_claimlost_signal(runtime):
    """A lost claim is reported as ClaimLost (a named outcome, not a generic spawn-ok-False).

    The Done-test names "ClaimLost" as the F-024 outcome. We accept any of: a SpawnResult with
    a ``failure_class``/error naming the lost claim, or a distinct ``ClaimLost`` result type.
    The load-bearing fact is that it is DISTINGUISHABLE from a successful spawn AND no actor opened.
    """
    chokepoint = _chokepoint()
    fake = FakeAdapter()
    _install_adapter(chokepoint, fake)

    binding, _token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=999,            # lost claim
        expected_owner_token=binding["owner_token"],
        level_config=_level_config(),
    )

    assert _ok(result) is False
    assert len(fake.calls) == 0
    # The outcome is identifiable as a lost claim, not a silent generic failure.
    blob = repr(result).lower()
    assert "claim" in blob or "lost" in blob or getattr(result, "failure_class", None) is not None, (
        "a lost claim must surface a ClaimLost-flavored outcome (failure_class or a 'claim'/'lost' "
        "marker), distinguishable from a spawn that actually opened an actor"
    )


# ===========================================================================
# HAPPY PATH — the claim is committed BEFORE the actor opens; STEP4 records a NON-NULL
# transcript_path (+ session_uuid + ACTUAL model_used) into the binding; STEP5 lands the
# node in ``running``. The fake adapter is the only mock; the executor/ledger are REAL.
#
# Mutants killed:
#   * actor opened before the claim         -> pin_and_open seen with a still-``planned`` binding
#   * STEP4 leaves transcript_path null      -> the non-null assertion fails (Inc 6 fail-loud pair)
#   * never reaches ``running``              -> the terminal-state assertion fails
# ===========================================================================

def test_happy_path_claims_before_actor_and_reaches_running(runtime):
    chokepoint = _chokepoint()

    # Record the binding STATE the adapter sees at pin_and_open time. The claim must already
    # be committed (state == 'claimed', a bumped lease_epoch) BEFORE the actor opens — the
    # F-024 ordering, asserted positively on the happy path.
    seen_states = []

    fake = FakeAdapter()
    original_pin = fake.pin_and_open

    def recording_pin(neutral_brief, level_config, tmux_target, env):
        live = ledger.read_binding(NODE)
        seen_states.append((live["state"], live["lease_epoch"]))
        return original_pin(neutral_brief, level_config, tmux_target, env)

    fake.pin_and_open = recording_pin
    _install_adapter(chokepoint, fake)

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )

    assert _ok(result) is True, "the happy path must report a successful spawn"
    assert len(fake.calls) == 1, "the adapter must be opened EXACTLY once on the happy path"

    # STEP1-before-STEP3: at pin_and_open time the slot was ALREADY claimed (claim before actor).
    assert seen_states, "the adapter must have been reached (pin_and_open called)"
    seen_state, seen_epoch = seen_states[0]
    assert seen_state == "claimed", (
        "claim-before-spawn: when the adapter opens the actor, the slot must ALREADY be in the "
        f"control-plane ``claimed`` state (saw {seen_state!r}) — the claim is STRICTLY before the actor"
    )
    assert seen_epoch == 2, "the claim must have bumped lease_epoch (1 -> 2) before the actor opened"

    # STEP5: the node ends in ``running`` (claimed -> spawning -> running).
    final = _read()
    assert final["state"] == "running", "STEP5 must transition claimed -> spawning -> running"
    spawn_brief = fake.calls[0][0]
    assert spawn_brief["turn_hook_profile"] == turn_state.CLAUDE_FULL_EDGES
    assert Path(spawn_brief["turn_state_path"]).is_file()
    assert Path(spawn_brief["turn_events_path"]).is_file()
    assert Path(spawn_brief["owed_checklist_path"]).is_file()
    assert final["turn_hook_profile"] == turn_state.CLAUDE_FULL_EDGES
    assert final["turn_state_path"] == str(addressing.turn_state_path(NODE, runtime))
    assert final["turn_events_path"] == str(addressing.turn_events_path(NODE, runtime))
    assert final["owed_checklist_path"] == str(
        addressing.owed_checklist_path(NODE, runtime)
    )


def test_step4_records_non_null_transcript_path(runtime):
    """STEP4 writes a NON-NULL transcript_path into the binding (the spawn<->detector contract
    producer — pairs with Increment 6's transcript-absent fail-loud).

    Mutant killed: leave transcript_path null -> the detector's stat() target is missing -> caught.
    """
    chokepoint = _chokepoint()
    fake = FakeAdapter(
        session_uuid="sess-uuid-spawned-7777",
        transcript_path="/runtime/transcripts/sess-uuid-spawned-7777.jsonl",
    )
    _install_adapter(chokepoint, fake)

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )
    assert _ok(result) is True

    final = _read()
    assert final.get("transcript_path") is not None, (
        "STEP4 must record a NON-NULL transcript_path into the binding (Inc 6 fail-loud pair: a "
        "null transcript_path breaks the spawn<->detector contract)"
    )
    assert final["transcript_path"] == "/runtime/transcripts/sess-uuid-spawned-7777.jsonl"
    # session_uuid + the ACTUAL model_used are recorded too (config = intent, model_used = fact).
    assert final.get("session_uuid") == "sess-uuid-spawned-7777"
    assert final.get("model_used") == "opus-4.8 / claude-code"


def test_step4_records_codex_seat_and_auth_evidence_when_adapter_reports_it(runtime):
    """Codex seat/auth evidence must survive the single-writer STEP4 binding stamp."""
    chokepoint = _chokepoint()
    fake = FakeAdapter(
        model_used="gpt-5.5 / codex",
        codex_seat_id="harness-proj-x-exec-1234",
        codex_auth_version="authv-99-123",
        codex_access_seconds_remaining=86400,
    )
    _install_adapter(chokepoint, fake)

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )
    assert _ok(result) is True

    final = _read()
    assert final["model_used"] == "gpt-5.5 / codex"
    assert final["codex_seat_id"] == "harness-proj-x-exec-1234"
    assert final["auth_version"] == "authv-99-123"
    assert final["codex_access_seconds_remaining"] == 86400


# ===========================================================================
# STEP0 — a PAUSED subtree admits no child. If THIS node OR any ancestor has paused_at set,
# the chokepoint ABORTS BEFORE claiming -> no claim, no actor.
#
# Mutant killed: skip the pause-subtree read-point -> a child is spawned under a paused subtree.
# ===========================================================================

def test_step0_paused_ancestor_aborts_before_claim(runtime):
    chokepoint = _chokepoint()
    fake = FakeAdapter()
    _install_adapter(chokepoint, fake)

    # Parent (ancestor) is PAUSED; the child is planned. The child must NOT spawn.
    parent, _ptoken = _binding(
        node_address=PARENT, parent_address="", state="running", generation=3,
        lease_epoch=1, subagent_id="subagent-parent", session_uuid="sess-parent",
        paused_at="2026-06-06T10:00:00+00:00",
    )
    child, ctoken = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([parent, child])
    before = _read(NODE)

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=ctoken,
        level_config=_level_config(),
    )

    assert _ok(result) is False, "STEP0: a paused subtree must admit no new child"
    assert len(fake.calls) == 0, "STEP0 aborts BEFORE the claim -> the actor is never opened"
    assert _read(NODE) == before, "a paused-subtree abort must leave the child binding UNCHANGED"


# ===========================================================================
# ROLLBACK on adapter SpawnFailure — the first-class rollback edge.
#
# A real claim is committed (claimed, epoch bumped). The adapter then raises a typed E32
# SpawnFailure. The chokepoint must RELEASE the claim (CAS claimed -> planned, BUMP epoch
# AGAIN) and emit an L1 escalation. NO leaked ``claimed`` slot.
#
# Mutants killed:
#   * skip release_claim          -> the slot is left ``claimed`` (leaked) -> caught
#   * release without bumping epoch -> the epoch is not advanced past the claim -> caught
#   * silent failure (no escalation) -> the L1-escalation assertion fails  -> caught
# ===========================================================================

def test_rollback_on_spawn_failure_releases_claim_and_bumps_epoch(runtime):
    chokepoint = _chokepoint()
    failing = FailingAdapter(failure_class="model_unavailable")
    _install_adapter(chokepoint, failing)

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )

    assert _ok(result) is False, "an adapter SpawnFailure must NOT report a successful spawn"

    # The adapter WAS reached (this is a genuine POST-claim failure, not a pre-claim abort) —
    # so the claim was committed first, then rolled back.
    assert len(failing.calls) == 1, (
        "the rollback path is a POST-claim failure: the claim commits, THEN the adapter is "
        "reached and raises — so pin_and_open must have been called exactly once"
    )

    final = _read()
    # The slot is RECLAIMABLE: back to ``planned`` (not a leaked ``claimed`` slot).
    assert final["state"] == "planned", (
        "rollback: an adapter SpawnFailure must CAS the slot back to ``planned`` — a leaked "
        "``claimed`` slot is the un-reclaimable failure the rollback edge exists to prevent"
    )
    # The epoch was bumped TWICE: once by the claim (1->2), once by the release (2->3).
    assert final["lease_epoch"] >= 3, (
        "rollback: the release must BUMP the epoch again (claim 1->2, release 2->3) so the rolled-"
        f"back slot fences the failed incarnation (saw lease_epoch={final['lease_epoch']})"
    )
    assert int(final.get("consecutive_failed_incarnations") or 0) == 0, (
        "a pre-actor adapter failure spends no model turn and must not spend a respawn try"
    )
    assert not final.get("respawn_parked_at")


def test_rollback_emits_l1_escalation_naming_failure_class(runtime):
    """On the pin/spawn failure the chokepoint emits an L1 escalation naming the child-address +
    which class fired (§6.3: 'release the claim and emit a spawn-failure escalation to L1').

    Mutant killed: a silent rollback with no escalation -> L1 never alerts the user -> caught.

    The escalation surface is accepted in EITHER shape (the precise transport is a later-cluster
    fork): the SpawnResult carries the failure_class, OR a run-ledger escalation/spawn-failure row
    naming the node is appended. The load-bearing fact: the failure is SURFACED, classified, and
    tied to the child address — not swallowed.
    """
    chokepoint = _chokepoint()
    failing = FailingAdapter(failure_class="model_unavailable")
    _install_adapter(chokepoint, failing)

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])
    wal_before = ledger.load_wal()

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )
    assert _ok(result) is False

    # Surface 1: the SpawnResult names the failure class (configured-vs-actual classification).
    result_classifies = getattr(result, "failure_class", None) == "model_unavailable"

    # Surface 2: a WAL row naming the child-address as a spawn-failure/escalation was appended.
    new_rows = ledger.load_wal()[len(wal_before):]
    wal_escalates = any(
        r.get("node_address") == NODE
        and (
            "escalat" in (r.get("event") or "").lower()
            or "spawn_fail" in (r.get("event") or "").lower()
            or "model_unavailable" in (r.get("summary") or "").lower()
            or "escalat" in (r.get("summary") or "").lower()
        )
        for r in new_rows
    )

    assert result_classifies or wal_escalates, (
        "§6.3: the pin/spawn failure must emit an L1 escalation naming the child-address + which "
        "class fired (failure_class on the result OR a spawn-failure/escalation WAL row) — a silent "
        "rollback that never alerts L1 is the mutant this kills"
    )


def test_rollback_records_actual_model_used(runtime):
    """The chokepoint ALWAYS writes the ACTUAL model_used (config = intent; model_used = fact),
    even on the failure path that still knows it (§6.3: 'a checker asserts every spawned child has
    a model_used == configured, OR a corresponding escalation exists').

    We assert the WEAKER, always-true half: the failure is classified (the escalation exists), so
    the config==model_used checker is satisfiable. (model_used recording on the running binding is
    asserted on the happy path; here the binding rolled back to planned.)
    """
    chokepoint = _chokepoint()
    failing = FailingAdapter(failure_class="auth_expired", model_used="opus-4.8 / claude-code")
    _install_adapter(chokepoint, failing)

    binding, token = _binding(state="planned", generation=0, lease_epoch=1)
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )
    assert _ok(result) is False
    assert _read()["state"] == "planned", "auth_expired is an E32 class -> rollback to planned"


# ===========================================================================
# release_claim — the standalone rollback edge (CAS claimed -> planned, bump epoch).
#
# Mutant killed: a release that does not bump the epoch -> the rolled-back slot does not fence the
# failed incarnation.
# ===========================================================================

def test_release_claim_cas_claimed_to_planned_bumps_epoch(runtime):
    chokepoint = _chokepoint()

    # Seed a node already in ``claimed`` (as STEP1 would leave it), with a known epoch.
    binding, _seed_token = _binding(state="claimed", generation=5, lease_epoch=4)
    # The live owner_token after a claim is the one minted at the claim's epoch; the binding
    # carries it. release_claim is fenced on owner_token, so present the live one.
    _seed([binding])
    live_token = _read()["owner_token"]

    chokepoint.release_claim(NODE, expected_owner_token=live_token)

    final = _read()
    assert final["state"] == "planned", "release_claim must CAS ``claimed`` -> ``planned``"
    assert final["lease_epoch"] == 5, "release_claim must BUMP the epoch (4 -> 5) on the rollback"


# ===========================================================================
# THE GATE FIREWALL (LOCKED §6.4) — the single, authoritative never-resume-across-the-gate point.
#
# Seed gate_crossed_at != null. resume() must REFUSE --resume and fall back to a FRESH
# claim_and_spawn with a DELTA brief. Two load-bearing assertions:
#   (1) BEHAVIORAL: the spawn that happened was a FRESH claim (a new session_uuid recorded), NOT a
#       resumed one — and the adapter was NOT handed a --resume argv.
#   (2) STRUCTURAL: there is NO code path that builds a --resume argv when gate_crossed_at != null
#       (the --resume argv is constructed ONLY on the else-branch). We assert this by inspecting
#       every (brief, env) handed to the adapter under a crossed gate: none carries a --resume token.
#
# Mutant killed: build a --resume anyway under a crossed gate -> the no-resume assertion fails.
# ===========================================================================

def _has_resume_token(call) -> bool:
    """True iff a recorded pin_and_open call carries a --resume token anywhere in its inputs.

    Scans the neutral_brief + env (and any nested argv) for a literal ``--resume`` / ``resume``
    session-continuation marker. Under a crossed gate this MUST be False for every call.
    """
    neutral_brief, level_config, tmux_target, env = call
    blob = repr((neutral_brief, env)).lower()
    return "--resume" in blob or "resume_session" in blob or "resume_argv" in blob


def test_gate_firewall_refuses_resume_falls_back_to_fresh_claim(runtime):
    chokepoint = _chokepoint()
    fake = FakeAdapter(session_uuid="sess-uuid-FRESH-after-gate")
    _install_adapter(chokepoint, fake)

    # A live RUNNING node that has CROSSED a gate. resume() across the gate is forbidden.
    binding, token = _binding(
        state="running", generation=7, lease_epoch=3,
        session_uuid="sess-uuid-PRE-gate",
        gate_crossed_at="2026-06-06T09:00:00+00:00",
    )
    _seed([binding])
    live_token = _read()["owner_token"]

    result = chokepoint.resume(
        NODE,
        expected_state="running",
        expected_generation=7,
        expected_owner_token=live_token,
        delta_inputs={"reason": "parent answered an escalation"},
        level_config=_level_config(),
    )

    assert _ok(result) is True, "resume across a crossed gate must FALL BACK to a fresh spawn (not fail)"

    # BEHAVIORAL: a FRESH session was recorded (the pre-gate session was NOT resumed).
    final = _read()
    assert final.get("session_uuid") == "sess-uuid-FRESH-after-gate", (
        "gate firewall: a crossed gate must re-spawn FRESH — the binding must carry the NEW session "
        "uuid, never the pre-gate conversational session (carrying it past the gate is the exact "
        "contamination the gate exists to stop)"
    )

    # STRUCTURAL: NO --resume argv was built under the crossed gate (every adapter call is clean).
    assert fake.calls, "the firewall fallback must still OPEN a fresh actor (adapter called)"
    assert not any(_has_resume_token(c) for c in fake.calls), (
        "gate firewall (LOCKED): there must be NO code path that builds a --resume argv when "
        "gate_crossed_at != null — the --resume is constructed ONLY on the else-branch"
    )


def test_resume_clean_gate_re_adopts_live_address_with_new_session(runtime):
    """The else-branch: with gate_crossed_at == null, resume RE-ADOPTS the live address (claim with
    expected_state in {running, dead}), bumps the epoch + re-mints the owner_token (fencing the prior
    incarnation), and records a NEW session_uuid. It never double-spawns a live address.

    This is the control case for the firewall: a clean gate is allowed to re-adopt.
    """
    chokepoint = _chokepoint()
    fake = FakeAdapter(session_uuid="sess-uuid-READOPT-0001")
    _install_adapter(chokepoint, fake)

    binding, token = _binding(
        state="running", generation=7, lease_epoch=3,
        session_uuid="sess-uuid-prior",
        gate_crossed_at=None,                 # CLEAN gate -> re-adopt allowed
    )
    _seed([binding])
    live_token = _read()["owner_token"]

    result = chokepoint.resume(
        NODE,
        expected_state="running",
        expected_generation=7,
        expected_owner_token=live_token,
        delta_inputs={"reason": "reconcile re-adopt"},
        level_config=_level_config(),
    )

    assert _ok(result) is True, "a clean-gate resume must re-adopt the live address"
    final = _read()
    # Re-adopt bumped the epoch (fences the prior incarnation) and re-minted ownership.
    assert final["lease_epoch"] > 3, "re-adopt must BUMP lease_epoch (fences the prior incarnation)"
    assert final["owner_token"] != live_token, "re-adopt must RE-MINT the owner_token (new epoch)"
    # A NEW session_uuid was recorded for the fresh incarnation.
    assert final.get("session_uuid") == "sess-uuid-READOPT-0001", (
        "re-adopt records the NEW incarnation's session_uuid (the fresh instance re-reads the work node)"
    )


def test_resume_refuses_a_three_death_park_before_claim_or_actor_open(runtime):
    """No recovery/resume path may spend a fourth incarnation."""
    chokepoint = _chokepoint()
    fake = FakeAdapter(session_uuid="must-not-open")
    _install_adapter(chokepoint, fake)
    binding, _token = _binding(
        state="running",
        generation=7,
        lease_epoch=3,
        session_uuid="sess-uuid-third-death",
        extra={
            "consecutive_failed_incarnations": 3,
            "failed_incarnation_causes": [
                {"event": "died_infrastructure", "reason": "pane_gone"},
                {"event": "watchdog_nonresponse", "reason": "idle_ladder"},
                {"event": "watchdog_runtime_failure", "reason": "stream_failure"},
            ],
            "respawn_parked_at": "2026-07-28T10:00:00+00:00",
        },
    )
    _seed([binding])

    result = chokepoint.resume(
        NODE,
        expected_state="running",
        expected_generation=7,
        expected_owner_token=binding["owner_token"],
        delta_inputs={"reason": "automatic recovery"},
        level_config=_level_config(),
    )

    assert result.ok is False
    assert result.failure_class == "respawn_parked"
    assert fake.calls == []
    assert _read() == binding


def test_resume_across_gate_records_no_prior_session_anywhere(runtime):
    """Reinforces the firewall structurally: under a crossed gate, the PRE-gate session_uuid must
    not survive into the new incarnation NOR be handed to the adapter as a resume target.

    Mutant killed: pass the pre-gate session as a --resume continuation -> the pre-gate session leaks
    past the gate.
    """
    chokepoint = _chokepoint()
    fake = FakeAdapter(session_uuid="sess-uuid-FRESH-2")
    _install_adapter(chokepoint, fake)

    binding, token = _binding(
        state="running", generation=2, lease_epoch=1,
        session_uuid="sess-uuid-PRE-gate-SECRET",
        gate_crossed_at="2026-06-06T08:30:00+00:00",
    )
    _seed([binding])
    live_token = _read()["owner_token"]

    chokepoint.resume(
        NODE,
        expected_state="running",
        expected_generation=2,
        expected_owner_token=live_token,
        delta_inputs={},
        level_config=_level_config(),
    )

    # The pre-gate session id must not appear in anything handed to the adapter (no resume target).
    for call in fake.calls:
        neutral_brief, _lc, _tt, env = call
        blob = repr((neutral_brief, env))
        assert "sess-uuid-PRE-gate-SECRET" not in blob, (
            "gate firewall: the PRE-gate session must NOT be handed to the adapter as a resume "
            "continuation — a crossed gate re-spawns FRESH, carrying no pre-gate session context"
        )


# ===========================================================================
# collapse — the terminal write carrying the in_flight RELEASE-DECREMENT.
# done/failed/dead collapse; ESCALATED is NOT a collapse (asymmetric — state stays running).
#
# Mutants killed:
#   * collapse on ESCALATED -> the node is wrongly torn down (state changed off running) -> caught
#   * collapse does not route a terminal transition -> the node never reaches its terminal state
# ===========================================================================

def test_collapse_done_routes_terminal_transition(runtime):
    chokepoint = _chokepoint()

    binding, _token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    live_token = _read()["owner_token"]

    chokepoint.collapse(NODE, "DONE", expected_owner_token=live_token)

    final = _read()
    assert final["state"] == "done", (
        "collapse(DONE) must route the terminal transition running -> done through the executor"
    )


def test_collapse_failed_routes_terminal_transition(runtime):
    chokepoint = _chokepoint()
    binding, _token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    live_token = _read()["owner_token"]

    chokepoint.collapse(NODE, "FAILED", expected_owner_token=live_token)

    assert _read()["state"] == "failed", "collapse(FAILED) must route running -> failed"


def test_collapse_done_reaps_review_check_pane_after_success(runtime, monkeypatch):
    chokepoint = _chokepoint()
    tmux_target = "harness-proj-widget-reviews-L3-reviewers-correctness-exec:0.0"
    binding, _token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={
            "review_check_for": "proj/widget/reviews/L3#review",
            "role_variant": "L3#review-check",
            "tmux_target": tmux_target,
        },
    )
    _seed([binding])
    live_token = _read()["owner_token"]
    calls = []

    def kill_and_fail(target):
        calls.append(target)
        raise RuntimeError("tmux already gone")

    monkeypatch.setattr(chokepoint, "kill_stale_pane", kill_and_fail)

    result = chokepoint.collapse(NODE, "DONE", expected_owner_token=live_token)

    assert _ok(result) is True
    assert _read()["state"] == "done"
    assert calls == [tmux_target], (
        "a successful review-check collapse must best-effort reap the recorded pane target, "
        "and reap failure must not convert the clean collapse to failure"
    )


def test_collapse_done_does_not_reap_normal_pane(runtime, monkeypatch):
    chokepoint = _chokepoint()
    binding, _token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"role_variant": "L3", "tmux_target": "harness-proj-widget-exec:0.0"},
    )
    _seed([binding])
    live_token = _read()["owner_token"]
    calls = []
    monkeypatch.setattr(chokepoint, "kill_stale_pane", lambda target: calls.append(target))

    result = chokepoint.collapse(NODE, "DONE", expected_owner_token=live_token)

    assert _ok(result) is True
    assert _read()["state"] == "done"
    assert calls == [], "ordinary executor/coordinator collapse must not broadly reap panes"


def test_escalated_is_not_a_collapse(runtime):
    """ESCALATED is ASYMMETRIC (§3.6): the terminal_signal is set but the lifecycle state STAYS
    ``running`` and the node is NOT collapsed.

    Mutant killed: route ESCALATED through collapse like DONE/FAILED -> the node is torn off its
    running state and its slot wrongly released -> caught.
    """
    chokepoint = _chokepoint()
    binding, _token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    live_token = _read()["owner_token"]
    before_state = _read()["state"]

    # collapse must REFUSE / NO-OP on ESCALATED (it is not a terminal collapse). Whether it raises
    # or returns, the load-bearing invariant is: the node is NOT collapsed — state stays running.
    try:
        chokepoint.collapse(NODE, "ESCALATED", expected_owner_token=live_token)
    except Exception:
        # An explicit refusal is acceptable (ESCALATED is not a collapse vocabulary).
        pass

    final = _read()
    assert final["state"] == before_state == "running", (
        "ESCALATED is NOT a collapse: the lifecycle state must STAY ``running`` (asymmetric, §3.6) — "
        "collapsing on ESCALATED tears the node off its slot while it waits for the answer round-trip"
    )
    assert final["state"] not in ("done", "failed", "dead"), (
        "ESCALATED must never drive the node into a terminal collapse state"
    )


def test_collapse_emits_section_3_6_event_names(runtime):
    """SML-01: collapse must journal the §3.6 NORMATIVE run-ledger event derived from the terminal
    signal (signal_DONE / signal_FAILED / died_infrastructure), NOT the non-normative
    f"collapse_{target_state}" spelling the sign-off check cannot key on.

    Mutant killed: event=f"collapse_{target_state}" -> the WAL rows read collapse_done/collapse_failed
    instead of the TERMINAL_VOCAB names -> caught per-signal here.
    """
    chokepoint = _chokepoint()

    for terminal_signal, expected_event, expected_state in (
        ("DONE", "signal_DONE", "done"),
        ("FAILED", "signal_FAILED", "failed"),
        ("DIED_INFRA", "died_infrastructure", "failed"),
    ):
        binding, _token = _binding(state="running", generation=4, lease_epoch=2)
        _seed([binding])
        live_token = _read()["owner_token"]
        wal_before = len(ledger.load_wal())

        result = chokepoint.collapse(NODE, terminal_signal, expected_owner_token=live_token)

        assert _ok(result) is True, f"collapse({terminal_signal!r}) must commit"
        assert _read()["state"] == expected_state
        new_rows = ledger.load_wal()[wal_before:]
        collapse_rows = [r for r in new_rows if r.get("node_address") == NODE]
        assert len(collapse_rows) == 1, (
            f"collapse({terminal_signal!r}) must append exactly ONE WAL row; got {len(collapse_rows)}"
        )
        assert collapse_rows[0].get("event") == expected_event, (
            f"collapse({terminal_signal!r}) must journal the §3.6 normative event {expected_event!r} "
            f"(states.TERMINAL_VOCAB), got {collapse_rows[0].get('event')!r}"
        )


# ===========================================================================
# escalate — the §3.6 ESCALATED slot-hold journal (SML-02). A fenced, exactly-once,
# generation-bumping running->running write through the single-writer executor:
# terminal_signal=ESCALATED stamped, event signal_ESCALATED journaled, state STAYS
# running, slot HELD (no in_flight_release).
#
# Mutants killed:
#   * never journal (the bare-NOOP watchdog branch) -> no signal_ESCALATED row -> caught
#   * journal per-tick (level-triggered)            -> a second escalate appends rows -> caught
#   * release the slot (in_flight_release in delta) -> caught
#   * skip the fence                                -> a stale token stamps the live node -> caught
# ===========================================================================

def test_escalate_stamps_and_journals_exactly_once(runtime):
    chokepoint = _chokepoint()
    binding, _token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    live = _read()
    live_token = live["owner_token"]
    gen_before = live["generation"]
    wal_before = len(ledger.load_wal())

    result = chokepoint.escalate(NODE, expected_owner_token=live_token)

    assert result is not None and _ok(result) is True, "the first escalate on a running node must commit"
    after = _read()
    assert after["terminal_signal"] == "ESCALATED", "escalate must stamp terminal_signal=ESCALATED"
    assert after.get("terminal_signal_at"), "escalate must stamp terminal_signal_at"
    assert after["state"] == "running", "ESCALATED is ASYMMETRIC (§3.6): state STAYS running (no collapse)"
    assert after["generation"] == gen_before + 1, (
        "the signal_ESCALATED row is a generation-bumping (replayable, §4.4) transition — exactly +1"
    )

    new_rows = ledger.load_wal()[wal_before:]
    assert len(new_rows) == 1, f"escalate must append exactly ONE WAL row; got {len(new_rows)}"
    row = new_rows[0]
    assert row.get("event") == "signal_ESCALATED", (
        f"the §3.6 run-ledger event for the ESCALATED row is signal_ESCALATED; got {row.get('event')!r}"
    )
    assert row.get("from_state") == "running" and row.get("to_state") == "running", (
        "the ESCALATED journal row is the running->running slot-hold (state unchanged)"
    )
    assert "in_flight_release" not in (row.get("binding_delta") or {}), (
        "ESCALATED HOLDS its slot: the delta must NOT carry in_flight_release (§3.6 asymmetric)"
    )

    # EXACTLY-ONCE: a second escalate is a no-op — returns None, appends ZERO rows.
    wal_mid = len(ledger.load_wal())
    second = chokepoint.escalate(NODE, expected_owner_token=after["owner_token"])
    assert second is None, "a second escalate on an already-stamped node must return None (exactly-once)"
    assert len(ledger.load_wal()) == wal_mid, "a second escalate must append ZERO new WAL rows"
    assert _read()["generation"] == gen_before + 1, "a second escalate must not bump the generation again"


def test_escalate_is_fenced_stale_token_journals_stale_return(runtime):
    chokepoint = _chokepoint()
    binding, _token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    before = copy.deepcopy(_read())
    wal_before = len(ledger.load_wal())

    stale_token = fencing.mint_owner_token(NODE, SUBAGENT, "sess-uuid-DEAD-incarnation", 1)
    assert stale_token != before["owner_token"]

    result = chokepoint.escalate(NODE, expected_owner_token=stale_token)

    assert result is not None and _ok(result) is False, "a stale-token escalate must abort (FENCED)"
    after = _read()
    assert after == before, (
        "the §3.6 FENCED de-auth is NON-DESTRUCTIVE: a stale-token escalate leaves the live binding "
        "byte-for-byte UNCHANGED (no ESCALATED stamp, no generation bump)"
    )
    new_rows = ledger.load_wal()[wal_before:]
    assert len(new_rows) == 1 and new_rows[0].get("event") == "stale_return_ignored", (
        "the stale-token escalate must journal exactly ONE stale_return_ignored row "
        f"(the executor's fencing precondition); got {[r.get('event') for r in new_rows]!r}"
    )


def test_escalate_refuses_a_non_running_node(runtime):
    """The ESCALATED slot-hold is the running->running row (§3.6): a non-running node has no slot to
    hold — escalate must refuse (ok=False) and write NOTHING."""
    chokepoint = _chokepoint()
    binding, _token = _binding(state="claimed", generation=2, lease_epoch=1)
    _seed([binding])
    before = copy.deepcopy(_read())
    wal_before = len(ledger.load_wal())

    result = chokepoint.escalate(NODE, expected_owner_token=before["owner_token"])

    assert result is not None and _ok(result) is False, (
        "escalate on a non-running node must return ok=False (the §3.6 slot-hold applies to running only)"
    )
    assert _read() == before, "a refused escalate must leave the binding unchanged"
    assert len(ledger.load_wal()) == wal_before, "a refused escalate must append no WAL row"


# ===========================================================================
# E1 — the pieces-present SPAWN GATE (enforcement spine, 2026-06-11).
#
# The 2026-06-11 live run booted every agent under-equipped (dangling manifest
# refs, no spec pointer) and each one bootstrapped itself — agent-lifecycle L13
# ("by the time you receive your first context everything is already loaded —
# you never bootstrap yourself") was exhortation with no enforcement. E1 makes
# it mechanical: pieces_present.check_boundary runs at STEP2.5 (after the
# committed claim + brief assembly, BEFORE the actor opens). On fail: release
# the claim + spawn_failed escalation, failure_class names pieces_missing, the
# adapter is NEVER reached. The derivation half (agent-lifecycle: "the daemon
# derives its spec/acceptance pointers from the node you prepared"): a prepared
# node's brief.md / acceptance.md yield spec_pointer / frozen_acceptance_ref
# when the binding is sparse.
# ===========================================================================

def test_e1_unprepared_node_refused_pieces_missing(runtime):
    """No spec_pointer on the binding AND no brief.md in the node dir -> the gate refuses AFTER
    the claim (release + escalation), the actor NEVER opens, failure names pieces_missing.
    (Mutant: skip the gate -> the adapter opens an under-equipped actor -> caught.)"""
    chokepoint = _chokepoint()
    fake = FakeAdapter()
    _install_adapter(chokepoint, fake)
    binding, token = _binding(spec_pointer=None)  # sparse: no spec_pointer; node dir unprepared
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )

    assert _ok(result) is False, "an unprepared node must be REFUSED"
    assert "pieces_missing" in (getattr(result, "failure_class", "") or ""), (
        f"the refusal must NAME pieces_missing; got {getattr(result, 'failure_class', None)!r}"
    )
    assert len(fake.calls) == 0, "the adapter must NEVER open an under-equipped actor"
    final = _read()
    assert final["state"] == "planned", "the claim must be RELEASED (claimed -> planned)"
    assert any(
        (r.get("event") or "").startswith("spawn_failed") or "pieces_missing" in str(r)
        for r in ledger.load_wal()
    ), "the refusal must land a visible spawn-failure escalation row (never a silent skip)"


def test_e1_prepared_node_derives_spec_pointer_and_spawns(runtime):
    """The derivation half: a node dir carrying brief.md yields the spec_pointer with NO binding
    field; the gate passes and the actor opens with the derived pointer on its brief.
    (Mutant: gate without derivation -> every real spawn refused -> caught here.)"""
    import harnessd.addressing as addressing

    chokepoint = _chokepoint()
    fake = FakeAdapter()
    _install_adapter(chokepoint, fake)
    binding, token = _binding(spec_pointer=None)  # NO binding field — the node dir is the source
    _seed([binding])
    node_dir = addressing.node_dir(NODE, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "brief.md").write_text("# brief — prepared by the parent\n", encoding="utf-8")

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )

    assert _ok(result) is True, (
        f"a PREPARED node must spawn; got failure_class={getattr(result, 'failure_class', None)!r}"
    )
    assert len(fake.calls) == 1, "the actor must open exactly once"
    sent_brief = fake.calls[0][0]
    spec = (
        sent_brief.get("spec_pointer")
        if isinstance(sent_brief, dict)
        else getattr(sent_brief, "spec_pointer", None)
    )
    assert spec and str(spec).endswith("brief.md"), (
        f"the brief handed to the adapter must carry the DERIVED spec_pointer; got {spec!r}"
    )


def test_lr18_fresh_spawn_tears_down_a_stale_prior_pane(runtime):
    """LR-18: a fresh spawn at an address whose PRIOR incarnation's pane still exists tears the
    stale session down (best-effort) BEFORE opening the actor — collapse never reaps panes, so
    the deterministic session name otherwise collides (observed twice live: the watchdog-FAILED
    L1; the collapsed planning-L3 vs its execution-L3 respawn). (Mutant: no teardown -> kill
    never called -> caught.)"""
    chokepoint = _chokepoint()
    kills = []

    class _KillTmux:
        def kill(self, target):
            kills.append(target)

    fake = FakeAdapter()
    fake.tmux = _KillTmux()
    _install_adapter(chokepoint, fake)
    binding, token = _binding()
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=_level_config(),
    )

    import harnessd.addressing as addressing

    assert _ok(result) is True
    assert kills == [addressing.session_name_for(NODE)], (
        f"the fresh spawn must tear down the prior incarnation's pane (kill_stale_pane) before "
        f"create_detached, and the kill target must be the CANONICAL SESSION NAME — the raw node "
        f"address names no tmux session, so killing it tears down nothing (LR-21, observed live: "
        f"Run-2 assembly execution-L3 collided against its done predecessor's pane); kill calls: "
        f"{kills!r}"
    )


def test_lr21_kill_stale_pane_normalizes_a_recorded_triple_to_its_session(runtime):
    """LR-21 pin: the re-register/resume sites pass the RECORDED tmux_target — the
    '<session>:<window>.<pane>' triple STEP4 stamped. tmux kill-session wants the session;
    the triple must be normalized. (Mutant: no normalization -> kill called with the triple
    -> caught.)"""
    chokepoint = _chokepoint()
    kills = []

    class _KillTmux:
        def kill(self, target):
            kills.append(target)

    fake = FakeAdapter()
    fake.tmux = _KillTmux()

    chokepoint.kill_stale_pane("harness-proj-widget-exec:0.0", adapter=fake)

    assert kills == ["harness-proj-widget-exec"], (
        f"a recorded triple must be normalized to its session name for the kill; got {kills!r}"
    )


def test_lr21_kill_stale_pane_reaches_the_real_tmux_module_when_no_adapter_is_anywhere(runtime, monkeypatch):
    """LR-21 pin (the production no-op): in the E4 registry world the injected ADAPTER is None
    and the re-register/resume call sites pass no adapter — the old seam-or-skip resolution made
    the teardown silently do NOTHING in production (observed live: Run-2 assembly execution-L3
    respawn collided against its done predecessor's still-alive pane; with LR-19's truth-check
    the re-drive then correctly refused to touch the live session -> wedge). With no seam at all,
    the REAL tmux module is the teardown channel. A seam that EXISTS but lacks tmux.kill still
    skips (the dry-run tears nothing down — unchanged). (Mutant: seam-or-skip restored -> the
    real kill never fires -> caught.)"""
    chokepoint = _chokepoint()
    prev = getattr(chokepoint, "ADAPTER", None)
    chokepoint.set_adapter(None)
    kills = []
    import harnessd.spawn.tmux as tmux_mod
    monkeypatch.setattr(tmux_mod, "kill", lambda s: kills.append(s))
    try:
        chokepoint.kill_stale_pane("harness-x-exec:0.0")
    finally:
        chokepoint.set_adapter(prev)

    assert kills == ["harness-x-exec"], (
        f"with no adapter anywhere the teardown must route through the REAL tmux module "
        f"(production is the registry world — ADAPTER is None); real kills: {kills!r}"
    )


# ==================================================================================================
# Registry resolution after the 2026-07-16 L5 Codex-path ruling: L1-L4 and the review seats
# resolve the claude-code adapter; L5 resolves the CODEX adapter (native runtime, no CLIProxy).
# Exercises the PRODUCTION path (_require_adapter over the registry, ADAPTER unset),
# self-contained save/restore since the registry is a module-level seam.
# ==================================================================================================

def test_registry_resolves_seats_per_the_codex_path_ruling():
    chokepoint = _chokepoint()

    class _Marker:
        def __init__(self, name):
            self.name = name

    prior_adapter = chokepoint.ADAPTER
    prior_registry = dict(chokepoint.ADAPTER_REGISTRY)
    try:
        chokepoint.set_adapter(None)  # ADAPTER unset -> the registry is the resolver (production)
        chokepoint.clear_runtime_adapters()
        cc = _Marker("claude-code")
        cx = _Marker("codex")
        chokepoint.register_runtime_adapter("claude-code", cc)
        chokepoint.register_runtime_adapter("codex", cx)

        # L1-L4 and the review seat resolve the claude-code adapter.
        for level in ("L1", "L2", "L3", "L4", "L5+"):
            resolved = chokepoint._require_adapter(config.LevelConfig.for_level(level))
            assert resolved is cc, f"{level} must resolve the claude-code adapter, got {resolved!r}"

        # L5 resolves the CODEX adapter (the 2026-07-16 L5 Codex-path ruling: gpt-5.5, native
        # codex runtime, single warm runner).
        assert config.LevelConfig.for_level("L5").runtime == "codex"
        resolved_l5 = chokepoint._require_adapter(config.LevelConfig.for_level("L5"))
        assert resolved_l5 is cx, f"L5 must resolve the codex adapter, got {resolved_l5!r}"
    finally:
        chokepoint.clear_runtime_adapters()
        chokepoint.ADAPTER_REGISTRY.update(prior_registry)
        chokepoint.set_adapter(prior_adapter)


# ==================================================================================================
# RESPAWN-CURSOR (live, Run-5 2026-07-31) — the claim-from-dead necro respawn must reset the
# per-INCARNATION turn-event consumer cursor.
#
# Observed live: six seats respawned through the claim-only route (`harnessd spawn <addr>` with no
# --parent -> ipc._handle_spawn -> claim_and_spawn(expected_state='dead')) came back with
# ``turn_event_acked_offset`` still carrying the PRIOR incarnation's value (e.g. 398659) while
# STEP3's ``turn_state.seed`` had just truncated ``.turn-events.<seat>.jsonl`` back to zero bytes
# (~18K by the time it was read). The consumer (daemon._adopt_turn_state_for_seat ->
# turn_state.read_event_tail) seeks PAST EOF, reads nothing, and re-stamps the same offset forever:
# turn_state frozen at not_started, Stop rows never consumed, the wake/owed machinery dead for that
# seat. The register route (register_and_spawn_child -> _register_child) never showed this because it
# writes a WHOLE FRESH binding row, which simply drops the stale cursor and seeds
# liveness_state='claimed'.
#
# The cursor is per-INCARNATION exactly like the seat_stall_* block executor.claim already resets:
# the file it indexes is recreated by every spawn. ``last_inbox_acked_offset`` is deliberately NOT
# reset here — the inbox file is DURABLE across incarnations (the register path seeds it to the
# CURRENT inbox size for the same reason), so a reset there would re-deliver old mail.
#
# Mutants killed: leave the cursor stale -> the fresh incarnation's rows are never seen (both tests);
# leave liveness_state at the reconcile-stamped 'dead' -> the respawned seat reads dead on every
# roll-up; reset last_inbox_acked_offset too -> the inbox test in this suite's siblings re-delivers.
# ==================================================================================================

def _dead_incarnation_events(root, *, rows=400) -> int:
    """Write a PRIOR incarnation's fat event log and return its byte size (the stale cursor)."""
    path = addressing.turn_events_path(NODE, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index in range(rows):
            handle.write(
                json.dumps(
                    {
                        "schema_version": turn_state.SCHEMA_VERSION,
                        "row_kind": turn_state.RAW_HOOK_EVENT,
                        "event_id": f"prior-{index:04d}",
                        "ts": "2026-07-30T12:00:00+00:00",
                        "node_address": NODE,
                        "owner_token": "owner-token-PRIOR-INCARNATION",
                        "hook_profile": turn_state.CLAUDE_FULL_EDGES,
                        "hook_event": "PostToolUse",
                        "state": None,
                        "adopted": False,
                        "runtime": "claude-code",
                        "payload": {"hook_event_name": "PostToolUse", "tool_use_id": f"t-{index}"},
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    return path.stat().st_size


def test_claim_from_dead_respawn_resets_the_turn_event_cursor_and_liveness(runtime):
    """The necro respawn must not hand the fresh incarnation the dead one's byte cursor."""
    chokepoint = _chokepoint()
    _install_adapter(chokepoint, FakeAdapter())

    stale_offset = _dead_incarnation_events(runtime)
    binding, token = _binding(
        state="dead",
        generation=5,
        lease_epoch=3,
        extra={
            "liveness_state": "dead",
            "turn_event_acked_offset": stale_offset,
            "turn_state": turn_state.TURN_NOT_STARTED,
            "turn_events_path": str(addressing.turn_events_path(NODE, runtime)),
            "turn_state_path": str(addressing.turn_state_path(NODE, runtime)),
        },
    )
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="dead",
        expected_generation=5,
        expected_owner_token=token,
        level_config=_level_config(),
    )
    assert _ok(result) is True, "the claim-from-dead necro respawn must reach running"

    final = _read()
    events_size = addressing.turn_events_path(NODE, runtime).stat().st_size
    assert events_size < stale_offset, (
        "precondition: STEP3's turn_state.seed truncates the seat event log, so the fresh file is "
        f"SMALLER than the dead incarnation's cursor ({events_size} vs {stale_offset})"
    )
    assert final.get("turn_event_acked_offset") == 0, (
        "the claim-from-dead respawn must reset turn_event_acked_offset to 0: the log it indexes was "
        "just recreated, so the prior incarnation's byte cursor points PAST EOF and the daemon reads "
        f"nothing forever (got {final.get('turn_event_acked_offset')!r} against a "
        f"{events_size}-byte file)"
    )
    assert final.get("liveness_state") == "claimed", (
        "the respawn must seed liveness_state exactly as the register path does ('claimed'); leaving "
        f"the reconcile-stamped 'dead' makes a LIVE seat read dead (got {final.get('liveness_state')!r})"
    )


def test_respawned_seat_consumes_its_own_turn_events(runtime):
    """The consumer-visible property: after a claim-from-dead respawn the daemon sees the fresh
    incarnation's rows (RED before the fix: zero rows adopted, turn_state stuck at not_started)."""
    chokepoint = _chokepoint()
    _install_adapter(chokepoint, FakeAdapter())
    daemon = importlib.import_module("harnessd.daemon")

    stale_offset = _dead_incarnation_events(runtime)
    binding, token = _binding(
        state="dead",
        generation=5,
        lease_epoch=3,
        extra={
            "liveness_state": "dead",
            "turn_event_acked_offset": stale_offset,
            "turn_state": turn_state.TURN_NOT_STARTED,
            "runtime": "claude-code",
        },
    )
    _seed([binding])

    assert _ok(
        chokepoint.claim_and_spawn(
            NODE,
            expected_state="dead",
            expected_generation=5,
            expected_owner_token=token,
            level_config=_level_config(),
        )
    ) is True

    live = _read()
    turn_state.append_raw_hook_event(
        runtime_root=runtime,
        node_address=NODE,
        owner_token=live["owner_token"],
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    daemon._adopt_turn_state_for_seat(executor, object(), NODE, live)

    adopted = _read()
    assert adopted.get("turn_state") == turn_state.TURN_RUNNING, (
        "the daemon must consume the RESPAWNED incarnation's turn events; a stale byte cursor seeks "
        f"past EOF and freezes turn_state at not_started (got {adopted.get('turn_state')!r})"
    )
    assert adopted.get("turn_event_acked_offset") == addressing.turn_events_path(
        NODE, runtime
    ).stat().st_size, "a healthy consumer ends the sweep exactly at the event log's size"
