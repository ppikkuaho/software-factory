"""Increment 5 — executor: the KEYSTONE single-writer transition primitive.

FROZEN ACCEPTANCE (TDD RED-first). Tests ONLY — no implementation. The executor
(``harnessd/executor.py``) does not exist yet; this file pins the contract it must
satisfy before a line of it is written.

Authoritative sources (grounded, not recalled):
  - IMPLEMENTATION-PLAN §2.6 — the frozen ``executor.py`` interface (exact signatures
    transcribed below) + the Increment-5 Done-test bullet (L703-709).
  - DAEMON §4.2 — the 3-precondition CAS (expected_state / per-node expected_generation /
    expected_owner_token), ALL checked BEFORE any mutation; legality gate; validate-before-commit.
  - DAEMON §4.3 — one EX serialization domain (the whole body runs inside ONE store.file_lock(EX)).
  - DAEMON §4.4 — intent-first commit ordering: append_wal (the INTENT) FIRST, then write_binding
    (the CHECKPOINT), so a crash between them leaves WAL-ahead-of-binding => REPLAYABLE.
  - DAEMON §3.2 / §3.5 — binding record + run-ledger WAL record field sets.
  - §2.6 unit-test bullet (L526-531): the three CAS aborts INDEPENDENTLY; the stale-token abort
    journals ``stale_return_ignored`` + leaves the live binding UNCHANGED; legality gate before any
    write; validate-before-commit; a legal transition commits + appends exactly one WAL record.

FROZEN INTERFACE (§2.6, transcribed):

    transition(node_address, *, expected_state, expected_generation, expected_owner_token,
               target_state, binding_delta, new_lease_epoch=None, new_owner_token=None,
               event, actor="harnessd", summary="", artifacts=None) -> TransitionResult
    claim(node_address, *, expected_state, expected_generation, expected_owner_token,
          level_config) -> TransitionResult            # transition variant: target_state='claimed',
                                                        # mints new owner_token + new lease_epoch.
    commit(candidate_binding, entry) -> None            # PRIVATE; intent-first; called only inside the
                                                        # held EX lock from transition().
    TransitionResult(ok, errors, warnings, binding)

PATH INJECTION (§2.4): the executor calls ``ledger.read_binding/append_wal/write_binding`` with NO
path arguments (see the §2.6 body), so it relies on ``ledger.RUNTIME_ROOT`` being bound. The
``executor_runtime`` fixture binds ``ledger.RUNTIME_ROOT = tmp_path`` for the duration of each test;
the executor's lock/WAL/binding files all land under that root.

LOAD-BEARING discipline (each test names the mutant it kills):
  * drop the expected_state check       -> a wrong-state call commits      -> caught
  * drop the expected_generation check  -> a wrong-generation call commits  -> caught
  * drop the expected_owner_token check -> a stale-token call commits       -> caught
  * mutate-then-check on the fence      -> stale abort corrupts the binding -> caught
  * forget to journal stale_return_ignored -> no FENCED row in the WAL      -> caught
  * write_binding BEFORE append_wal     -> a crash leaves binding-ahead-of-WAL (un-replayable) -> caught
  * commit-then-validate                -> a validate error leaves a written binding -> caught
"""

from __future__ import annotations

import copy

import pytest

import harnessd.config as config
import harnessd.executor as executor  # the keystone under construction (RED until it exists)
import harnessd.fencing as fencing
import harnessd.ledger as ledger


# ---------------------------------------------------------------------------
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so the executor's
# pathless ledger calls (read_binding/append_wal/write_binding) target the test
# tree. Restores the prior value so tests don't leak runtime state into one another.
# ---------------------------------------------------------------------------

@pytest.fixture
def executor_runtime(tmp_path):
    """Point ledger I/O at tmp_path for the duration of one test, then restore."""
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


NODE = "proj/widget#exec"
SUBAGENT = "subagent-aaaa1111"
SESSION = "sess-uuid-0001"


def _seed_running_binding(*, state="running", generation=4, lease_epoch=2):
    """Seed ONE live binding directly through the ledger (the §2.6 'write a binding map directly
    through ledger.write_binding(_lock_held=True)' seeding path), and return (binding, token).

    The seeded record carries the full CAS-bearing field set the executor reads: node_address,
    state, generation, lease_epoch, owner_token, subagent_id, session_uuid, last_applied_seq.
    """
    owner_token = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, lease_epoch)
    binding = {
        "node_address": NODE,
        "parent_address": "proj#exec",
        "level": "L3",
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": 0,
        "liveness_state": "working",
    }
    ledger.write_binding({NODE: copy.deepcopy(binding)}, _lock_held=True)
    return binding, owner_token


def _read(node=NODE):
    return ledger.read_binding(node)


def _result_ok(result) -> bool:
    """Read the .ok field off a TransitionResult (NamedTuple/dataclass/obj all expose .ok)."""
    return getattr(result, "ok")


# ===========================================================================
# CAS precondition 1 — expected_state. Vary ONLY the state; hold gen + token valid.
# Mutant killed: drop the expected_state check -> this wrong-state call commits.
# ===========================================================================

def test_cas_wrong_expected_state_aborts_independently(executor_runtime):
    binding, token = _seed_running_binding(state="running", generation=4)
    before = _read()

    result = executor.transition(
        NODE,
        expected_state="blocked",          # WRONG: the live state is 'running'
        expected_generation=4,             # correct
        expected_owner_token=token,        # correct
        target_state="done",               # a legal running->... edge, so only the state CAS can block
        binding_delta={"state": "done"},
        event="signal_DONE",
    )

    assert _result_ok(result) is False, "a wrong expected_state must ABORT the transition"
    assert _read() == before, "an aborted state-CAS must leave the binding byte-for-byte unchanged"


# ===========================================================================
# CAS precondition 2 — expected_generation (PER-NODE, not global len(ledger)).
# Vary ONLY the generation; hold state + token valid.
# Mutant killed: drop the per-node generation check -> a stale-generation call commits.
# ===========================================================================

def test_cas_wrong_expected_generation_aborts_independently(executor_runtime, monkeypatch):
    binding, token = _seed_running_binding(state="running", generation=4)
    before = _read()
    wal_before = ledger.load_wal()

    # ISOLATE the generation CAS from validate(): validate has its OWN generation post-condition
    # (candidate.generation == entry.expected_generation + 1) that would ALSO reject a wrong
    # expected_generation. To prove the *CAS precondition itself* is load-bearing (mutant: drop the
    # generation check), neutralize validate so the ONLY gate left is the CAS. With validate forced
    # to pass, dropping the generation check lets this stale-generation call COMMIT -> caught here.
    import harnessd.validate as validate_mod

    def passing_validate(candidate_binding, wal_tail):
        return ([], [])

    monkeypatch.setattr(validate_mod, "validate", passing_validate)
    if hasattr(executor, "validate"):
        monkeypatch.setattr(executor.validate, "validate", passing_validate, raising=False)

    result = executor.transition(
        NODE,
        expected_state="running",          # correct
        expected_generation=3,             # WRONG: the live per-node generation is 4
        expected_owner_token=token,        # correct
        target_state="done",               # legal running->done edge (so only the gen CAS can block)
        binding_delta={"state": "done"},
        event="signal_DONE",
    )

    assert _result_ok(result) is False, (
        "a wrong expected_generation must ABORT on the per-node CAS even when validate passes "
        "(mutant: drop the generation check -> a stale-generation call commits)"
    )
    assert _read() == before, "an aborted generation-CAS must leave the binding unchanged"
    # The CAS aborts before building/committing the candidate: no committing WAL row appended.
    new_rows = ledger.load_wal()[len(wal_before):]
    assert all(r.get("to_state") != "done" for r in new_rows), (
        "a generation-CAS abort must not append a committing WAL row"
    )


def test_cas_generation_is_per_node_not_global_ledger_length(executor_runtime):
    """PER-NODE generation, NOT global len(ledger). After committing several WAL rows on node A,
    a transition on node B must still CAS against B's OWN generation (0/4), never the global WAL
    length. Mutant killed: expected_generation compared to len(ledger) -> this passes spuriously."""
    # Node A: seed + drive a couple of commits so the global WAL length grows well past B's gen.
    a_binding, a_token = _seed_running_binding(state="running", generation=4)
    r1 = executor.transition(
        NODE, expected_state="running", expected_generation=4, expected_owner_token=a_token,
        target_state="blocked", binding_delta={"state": "blocked"}, event="block",
    )
    assert _result_ok(r1) is True, "control: a valid transition on node A must commit"

    # Node B: a SEPARATE node whose per-node generation is 0, seeded directly.
    node_b = "proj/other#exec"
    b_token = fencing.mint_owner_token(node_b, "subagent-bbbb2222", "sess-b", 1)
    b_record = {
        "node_address": node_b, "state": "running", "generation": 0, "lease_epoch": 1,
        "owner_token": b_token, "subagent_id": "subagent-bbbb2222", "session_uuid": "sess-b",
        "last_applied_seq": 0,
    }
    whole = ledger.all_nodes()
    whole[node_b] = b_record
    ledger.write_binding(whole, _lock_held=True)

    # Present B's TRUE per-node generation (0). If the executor were comparing to global
    # len(ledger) (now >= 1), this correct-per-node call would be wrongly rejected.
    result = executor.transition(
        node_b, expected_state="running", expected_generation=0, expected_owner_token=b_token,
        target_state="done", binding_delta={"state": "done"}, event="signal_DONE",
    )
    assert _result_ok(result) is True, (
        "expected_generation is PER-NODE: presenting node B's own generation (0) must commit "
        "even though the GLOBAL WAL length is larger (mutant: CAS vs len(ledger))"
    )


# ===========================================================================
# CAS precondition 3 — expected_owner_token (the fencing precondition).
# Vary ONLY the token; hold state + generation valid.
# Mutant killed: drop the owner_token check -> a STALE actor's call commits over the live owner.
# ===========================================================================

def test_cas_stale_owner_token_aborts_independently(executor_runtime):
    binding, live_token = _seed_running_binding(state="running", generation=4, lease_epoch=2)
    before = _read()
    # A token minted at a LOWER epoch (the prior incarnation) — the classic stale return.
    stale_token = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 1)
    assert stale_token != live_token

    result = executor.transition(
        NODE,
        expected_state="running",          # correct
        expected_generation=4,             # correct
        expected_owner_token=stale_token,  # WRONG: stale (lower-epoch) token
        target_state="done",
        binding_delta={"state": "done"},
        event="signal_DONE",
    )

    assert _result_ok(result) is False, "a stale owner_token must ABORT (fencing precondition)"
    assert _read() == before, (
        "the fencing abort is NON-DESTRUCTIVE: the live binding is left UNCHANGED "
        "(mutant: mutate-then-check corrupts it)"
    )


def test_stale_owner_token_abort_journals_stale_return_ignored(executor_runtime):
    """The stale-token abort JOURNALS a ``stale_return_ignored`` WAL row AND leaves the binding
    UNCHANGED (DAEMON §3.6 FENCED row; §2.6). Mutant killed: forget to journal -> no FENCED row."""
    binding, live_token = _seed_running_binding(state="running", generation=4, lease_epoch=2)
    before = _read()
    stale_token = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 1)

    wal_before = ledger.load_wal()

    result = executor.transition(
        NODE,
        expected_state="running",
        expected_generation=4,
        expected_owner_token=stale_token,
        target_state="done",
        binding_delta={"state": "done"},
        event="signal_DONE",
    )

    assert _result_ok(result) is False, "stale-token transition must abort"

    wal_after = ledger.load_wal()
    new_rows = wal_after[len(wal_before):]
    fenced_rows = [r for r in new_rows if r.get("event") == "stale_return_ignored"]
    assert fenced_rows, (
        "the stale-token abort must JOURNAL a 'stale_return_ignored' WAL row "
        "(non-destructive de-authorization, §3.6) — got new rows: "
        f"{[r.get('event') for r in new_rows]}"
    )
    assert fenced_rows[-1].get("node_address") == NODE
    # And the live binding is untouched (non-destructive).
    assert _read() == before, "the journaled fencing abort must leave the live binding UNCHANGED"


# ===========================================================================
# Legality gate — an illegal target_state aborts BEFORE any write.
# (running -> spawning is not in ALLOWED_TRANSITIONS.)
# ===========================================================================

def test_illegal_target_state_aborts_before_any_write(executor_runtime):
    binding, token = _seed_running_binding(state="running", generation=4)
    before = _read()
    wal_before = ledger.load_wal()

    result = executor.transition(
        NODE,
        expected_state="running",          # all CAS preconditions valid
        expected_generation=4,
        expected_owner_token=token,
        target_state="spawning",           # ILLEGAL: running -> spawning is not allowed
        binding_delta={"state": "spawning"},
        event="bogus",
    )

    assert _result_ok(result) is False, "an illegal target_state must ABORT (legality gate)"
    assert _read() == before, "an illegal-edge abort must leave the binding unchanged (nothing written)"
    # The legality gate aborts BEFORE the candidate is even built — no transition WAL row appended.
    wal_after = ledger.load_wal()
    new_states = [r.get("to_state") for r in wal_after[len(wal_before):]]
    assert "spawning" not in new_states, "an illegal transition must not append a committing WAL row"


# ===========================================================================
# Self-loop carve-out (SML-02): a from==to transition is NOT a forward edge — it falls
# through the legality gate to validate-before-commit, which admits ONLY the §3.6
# ESCALATED slot-hold (running->running with terminal_signal=ESCALATED) and ERRORS any
# other no-op. LOAD-BEARING after the gate relaxation: validate is the ONLY guard left
# against arbitrary no-op binding writes — a mutant deleting validate's no-op narrowing
# would commit the DONE self-loop below and is caught here.
# ===========================================================================

def test_non_escalated_self_loop_aborts_via_validate_with_nothing_written(executor_runtime):
    binding, token = _seed_running_binding(state="running", generation=4)
    before = _read()
    wal_before = ledger.load_wal()

    result = executor.transition(
        NODE,
        expected_state="running",
        expected_generation=4,
        expected_owner_token=token,
        target_state="running",                      # SELF-LOOP, but...
        binding_delta={"terminal_signal": "DONE"},   # ...NOT the ESCALATED slot-hold -> must abort
        event="bogus_self_loop",
    )

    assert _result_ok(result) is False, (
        "a non-ESCALATED self-loop must ABORT (validate's no-op narrowing: only the §3.6 ESCALATED "
        "slot-hold admits running->running)"
    )
    assert any("no-op" in e for e in getattr(result, "errors")), (
        f"the abort must carry validate's illegal-no-op error; got {getattr(result, 'errors')!r}"
    )
    assert _read() == before, "an aborted self-loop must leave the binding unchanged on disk"
    new_rows = ledger.load_wal()[len(wal_before):]
    assert [r for r in new_rows if r.get("event") == "bogus_self_loop"] == [], (
        "an aborted self-loop must not append a committing WAL row (validate-before-commit)"
    )


def test_escalated_self_loop_commits_with_warning(executor_runtime):
    """Companion control: the SAME self-loop carrying terminal_signal=ESCALATED is the §3.6 slot-hold
    — it commits (ok=True) with validate's benign warning, bumping the generation (replayable row)."""
    binding, token = _seed_running_binding(state="running", generation=4)
    wal_before = ledger.load_wal()

    result = executor.transition(
        NODE,
        expected_state="running",
        expected_generation=4,
        expected_owner_token=token,
        target_state="running",
        binding_delta={"terminal_signal": "ESCALATED"},
        event="signal_ESCALATED",
    )

    assert _result_ok(result) is True, (
        "the §3.6 ESCALATED slot-hold (running->running + terminal_signal=ESCALATED) must COMMIT "
        "through the relaxed gate (validate admits exactly this no-op)"
    )
    assert getattr(result, "warnings"), "the ESCALATED slot-hold surfaces validate's benign warning"
    after = _read()
    assert after["state"] == "running" and after["terminal_signal"] == "ESCALATED"
    assert after["generation"] == 5, "the slot-hold is a generation-bumping (replayable) transition"
    new_rows = ledger.load_wal()[len(wal_before):]
    assert [r.get("event") for r in new_rows] == ["signal_ESCALATED"], (
        f"exactly one signal_ESCALATED WAL row must land; got {[r.get('event') for r in new_rows]!r}"
    )


# ===========================================================================
# Validate-before-commit — when validate() returns an ERROR, the transition aborts with NOTHING
# written. The executor MUST call validate(candidate, wal_tail + [entry]) and honor an error verdict
# BEFORE commit(). We force the verdict by monkeypatching validate.validate to return an error on
# this candidate — a runtime-agnostic lever that does not depend on a specific binding_delta trick
# (the canonical §2.6 ordering — apply delta, THEN generation += 1 — would clobber a poisoned
# generation, so the delta route is not a reliable validate trigger). Mutant killed: commit-then-
# validate leaves a committed binding even though validate said no.
# ===========================================================================

def test_validate_error_aborts_with_nothing_written(executor_runtime, monkeypatch):
    binding, token = _seed_running_binding(state="running", generation=4)
    before = _read()
    wal_before = ledger.load_wal()

    import harnessd.validate as validate_mod

    def rejecting_validate(candidate_binding, wal_tail):
        return (["forced validate error for the validate-before-commit gate"], [])

    # Patch the name the executor resolves at call time (both the module attr and, if the executor
    # imported the symbol, its own reference) so the stub is honored however the executor binds it.
    monkeypatch.setattr(validate_mod, "validate", rejecting_validate)
    if hasattr(executor, "validate"):
        monkeypatch.setattr(executor.validate, "validate", rejecting_validate, raising=False)

    result = executor.transition(
        NODE,
        expected_state="running",          # all CAS preconditions valid
        expected_generation=4,
        expected_owner_token=token,
        target_state="done",               # a legal running->done edge
        binding_delta={"state": "done"},
        event="signal_DONE",
    )

    assert _result_ok(result) is False, "a candidate validate() rejects must ABORT (validate-before-commit)"
    assert getattr(result, "errors"), "an aborted-on-validate result must carry the validate errors"
    assert _read() == before, (
        "validate-before-commit: a validate ERROR leaves NOTHING written "
        "(mutant: commit-then-validate leaves a written binding)"
    )
    # No committing transition row landed for this aborted candidate.
    wal_after = ledger.load_wal()
    committed = [r for r in wal_after[len(wal_before):] if r.get("to_state") == "done"]
    assert committed == [], "validate-abort must not append a committing WAL row"


# ===========================================================================
# Happy path — a legal, CAS-valid, validating transition COMMITS:
#   binding updated, generation+1, exactly one WAL row appended, last_applied_seq stamped.
# ===========================================================================

def test_happy_path_transition_commits(executor_runtime):
    binding, token = _seed_running_binding(state="running", generation=4)
    wal_before = ledger.load_wal()

    result = executor.transition(
        NODE,
        expected_state="running",
        expected_generation=4,
        expected_owner_token=token,
        target_state="done",
        binding_delta={"state": "done", "liveness_state": "idle"},
        event="signal_DONE",
        summary="leaf signed off",
        artifacts=["report.md"],
    )

    assert _result_ok(result) is True, f"a valid transition must commit, got errors={getattr(result, 'errors', None)}"

    after = _read()
    assert after is not None, "the binding must still exist after a commit"
    assert after["state"] == "done", "the target_state must be applied to the binding"
    assert after["generation"] == 5, "generation must be bumped by exactly 1 (4 -> 5)"

    # Exactly ONE WAL row appended for this transition.
    wal_after = ledger.load_wal()
    new_rows = wal_after[len(wal_before):]
    assert len(new_rows) == 1, f"exactly one WAL record must be appended, got {len(new_rows)}: {new_rows}"
    entry = new_rows[0]
    assert entry["node_address"] == NODE
    assert entry["from_state"] == "running"
    assert entry["to_state"] == "done"
    assert entry["expected_generation"] == 4
    assert entry["generation"] == 5, "the WAL row records the POST-commit generation (= expected + 1)"

    # last_applied_seq stamped to the committed entry's seq (the replay watermark; §4.4).
    assert after["last_applied_seq"] == entry["seq"], (
        "the binding's last_applied_seq must be stamped to the committed WAL entry's seq "
        "(intent-first watermark, §4.4)"
    )


def test_happy_path_applies_arbitrary_binding_delta_fields(executor_runtime):
    """The binding_delta is merged into the candidate — non-state fields land too."""
    binding, token = _seed_running_binding(state="running", generation=4)
    result = executor.transition(
        NODE,
        expected_state="running",
        expected_generation=4,
        expected_owner_token=token,
        target_state="blocked",
        binding_delta={"state": "blocked", "liveness_state": "waiting"},
        event="block",
    )
    assert _result_ok(result) is True
    after = _read()
    assert after["state"] == "blocked"
    assert after["liveness_state"] == "waiting", "an arbitrary binding_delta field must be applied"


# ===========================================================================
# claim() — §6.1 STEP-1 slot-claim: a transition variant that mints a NEW owner_token and a
# NEW (bumped) lease_epoch, with target_state='claimed'. Here from a 'running' re-adopt edge.
# ===========================================================================

def test_claim_mints_new_token_and_bumped_epoch_targets_claimed(executor_runtime):
    binding, token = _seed_running_binding(state="running", generation=4, lease_epoch=2)
    old_epoch = binding["lease_epoch"]
    old_token = token

    level_config = config.get_level_config("L3")

    result = executor.claim(
        NODE,
        expected_state="running",          # the §6.4 resume-live re-adopt edge (running -> claimed)
        expected_generation=4,
        expected_owner_token=token,
        level_config=level_config,
    )

    assert _result_ok(result) is True, f"claim must commit, got errors={getattr(result, 'errors', None)}"
    after = _read()
    assert after["state"] == "claimed", "claim() targets the 'claimed' state"
    assert after["lease_epoch"] == old_epoch + 1, "claim() must advance the lease_epoch by exactly 1"
    assert after["owner_token"] != old_token, "claim() must mint a NEW owner_token (fences the prior incarnation)"
    # The new token is self-fencing: it embeds the bumped epoch as its trailing ':'-field.
    assert after["owner_token"].endswith(f":{old_epoch + 1}"), (
        "the minted owner_token must embed the bumped lease_epoch (self-fencing token, DAEMON §8)"
    )
    assert after["generation"] == 5, "claim() is a transition: generation is bumped by 1"


def test_claim_fences_the_prior_token(executor_runtime):
    """After a claim re-adopts the node (new epoch + token), the OLD token must no longer commit:
    a stale transition presenting the pre-claim token aborts (the claim fenced it)."""
    binding, old_token = _seed_running_binding(state="running", generation=4, lease_epoch=2)
    claim_result = executor.claim(
        NODE,
        expected_state="running",
        expected_generation=4,
        expected_owner_token=old_token,
        level_config=config.get_level_config("L3"),
    )
    assert _result_ok(claim_result) is True
    after_claim = _read()

    # The prior incarnation returns, presenting its OLD token against the now-claimed node.
    stale = executor.transition(
        NODE,
        expected_state="claimed",
        expected_generation=after_claim["generation"],
        expected_owner_token=old_token,    # STALE: the claim minted a higher-epoch token
        target_state="spawning",
        binding_delta={"state": "spawning"},
        event="spawn_ok",
    )
    assert _result_ok(stale) is False, "the pre-claim token must be fenced out after a re-adopt claim"


# ===========================================================================
# INTENT-FIRST crash-replayability (§4.4) — THE keystone durability case.
# Monkeypatch ledger.write_binding to RAISE *after* append_wal has landed (simulate a crash
# BETWEEN commit step-1 and step-2). Assert: the WAL entry IS present (the intent journaled) but
# the binding is UNCHANGED (the checkpoint never landed) => the event is REPLAYABLE.
#
# Mutant killed: write_binding BEFORE append_wal. Under that wrong ordering, a crash in append_wal
# would leave the binding ALREADY mutated with NO WAL row — binding-ahead-of-WAL, the un-replayable
# failure mode. This test wires the crash to fire on write_binding (step-2) and proves the WAL row
# is already on disk: that can ONLY be true if append_wal ran FIRST.
# ===========================================================================

def test_intent_first_crash_is_replayable(executor_runtime, monkeypatch):
    binding, token = _seed_running_binding(state="running", generation=4)
    before = _read()
    wal_before = ledger.load_wal()

    real_write_binding = ledger.write_binding

    def crashing_write_binding(candidate_map, *, _lock_held, binding_path=None):
        # Simulate a crash at commit step-2 (the binding CHECKPOINT), AFTER step-1 (append_wal)
        # has already durably landed the INTENT. The binding atomic-replace never happens.
        raise OSError("simulated crash during write_binding (commit step-2)")

    monkeypatch.setattr(ledger, "write_binding", crashing_write_binding)

    # The transition must surface the crash (raise) OR return not-ok; either way the durable
    # postcondition below is what makes the failure REPLAYABLE rather than lost.
    with pytest.raises(OSError):
        executor.transition(
            NODE,
            expected_state="running",
            expected_generation=4,
            expected_owner_token=token,
            target_state="done",
            binding_delta={"state": "done"},
            event="signal_DONE",
        )

    # Restore the real writer so the assertions read true on-disk state.
    monkeypatch.setattr(ledger, "write_binding", real_write_binding)

    # (1) The INTENT is journaled: a committing WAL row for this transition IS on disk.
    wal_after = ledger.load_wal()
    new_rows = wal_after[len(wal_before):]
    committing = [r for r in new_rows if r.get("to_state") == "done" and r.get("node_address") == NODE]
    assert committing, (
        "INTENT-FIRST: append_wal must run BEFORE write_binding, so a crash in write_binding still "
        "leaves the WAL row on disk. No WAL row => write_binding ran first (the un-replayable mutant)."
    )

    # (2) The CHECKPOINT never landed: the binding is byte-for-byte UNCHANGED.
    after = _read()
    assert after == before, (
        "the binding atomic-replace never landed (the crash hit step-2), so the live binding must be "
        "UNCHANGED — WAL-ahead-of-binding is the REPLAYABLE state"
    )
    assert after["generation"] == before["generation"], "generation must NOT have advanced (no checkpoint)"

    # (3) Therefore the event is replayable: its seq is strictly greater than the binding's watermark.
    assert committing[-1]["seq"] > after.get("last_applied_seq", 0), (
        "the journaled-but-uncheckpointed event has seq > binding.last_applied_seq => it is replayable "
        "(the §4.4 replay predicate)"
    )
