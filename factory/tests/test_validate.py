"""Increment 4 — validate.py acceptance (per-node admission discipline).

FROZEN acceptance for the PURE function::

    harnessd.validate.validate(candidate_binding: dict, wal_tail: list[dict])
        -> tuple[list[str], list[str]]   # (errors, warnings)

Contract (IMPLEMENTATION-PLAN §2.7 + DAEMON §4.2 + the recovered validate() L618 discipline):

  * PURE — returns the two lists; it writes NOTHING. The executor folds it into the legality
    gate and REFUSES to commit when ``errors`` is non-empty (DAEMON §4.2 line 635:
    "if errors: abort — validate-before-commit: NOTHING written").
  * errors BLOCK commit; warnings ALLOW commit.
  * Per-node admission ONLY — NOT the 400-line reviewer-loop / workboard schema; NO cross-file
    workboard checks. Only the *discipline* (flat errors/warnings, candidate + wal-tail) ports.

The validate() call site (executor.transition, §2.6) passes ``wal_tail + [entry]``: the LAST WAL
record is the about-to-commit entry, carrying ``from_state`` / ``to_state`` / ``expected_generation``
/ ``generation`` for THIS transition. The candidate binding is the post-mutation node record
(``state == entry.to_state``, ``generation == expected_generation + 1``).

What validate CHECKS (grounded in §4.2 + recovered discipline; FORK noted below):
  - ERROR: an ILLEGAL lifecycle transition (states.is_legal(from_state, to_state) is False).
           This is the headline "illegal transition rejected before write".
  - ERROR: a MALFORMED candidate — missing required binding fields, OR
           generation != expected_generation + 1 (the per-node CAS post-condition, §4.2).
  - WARNING: lesser issues that still ALLOW commit.

FORK (noted, not silently chosen): the §3.6/§4.2 text fixes the legality-error and the
post-commit generation invariant as the load-bearing ERRORS, and uses warnings for "lesser
issues" without enumerating an exact lesser-issue set for per-node v1. These tests therefore
pin the two normative ERRORS hard, pin "a valid candidate -> empty errors" hard, and assert the
warnings CHANNEL exists and allows (a benign anomaly surfaces as a warning, errors stay empty)
WITHOUT over-pinning which specific anomaly a builder routes to warnings. A builder may add
further admission errors; these tests only require the spec-mandated ones and the warnings-allow
behavior.
"""

import pytest

import harnessd.states as states
import harnessd.ledger as ledger

# Inc-4 deliverable. A HARD import (not importorskip): until validate.py exists this is a
# collection-time ImportError -> every test in this module is RED, which is the intended
# "validate.py missing => acceptance fails" signal (NOT a skip, which would hide it).
from harnessd.validate import validate


# ---------------------------------------------------------------------------
# Fixtures: a well-formed candidate binding + its about-to-commit WAL entry.
# Built with the REAL ledger.build_wal_record so the wal_tail is byte-faithful
# to what executor.transition actually hands validate().
# ---------------------------------------------------------------------------

NODE = "payments/gateway/stripe-client#exec"
_OWNER = "payments/gateway/stripe-client#exec:subagent-0a1b2c3d:3f2a9c81:3"


def _candidate(*, state, generation):
    """A minimally-complete post-mutation node binding (§3.2 required fields)."""
    return {
        "node_address": NODE,
        "parent_address": "payments/gateway#exec",
        "level": "L5",
        "state": state,
        "generation": generation,
        "lease_epoch": 3,
        "owner_token": _OWNER,
        "last_applied_seq": 412,
        "liveness_state": "working",
        "terminal_signal": None,
        "deliverable_state": "active",
    }


def _entry(*, from_state, to_state, expected_generation, generation, event, binding_delta=None):
    """The about-to-commit WAL record (wal_tail[-1]), via the real builder."""
    return ledger.build_wal_record(
        node_address=NODE,
        event=event,
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_generation,
        generation=generation,
        lease_epoch=3,
        owner_token=_OWNER,
        binding_delta=binding_delta if binding_delta is not None else {"state": to_state},
        summary="",
        artifacts=[],
        seq=413,
    )


def _legal_case():
    """A LEGAL running->done transition: candidate + wal_tail = [..., entry]."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="done", generation=gen)
    entry = _entry(
        from_state="running",
        to_state="done",
        expected_generation=expected_gen,
        generation=gen,
        event="signal_DONE",
    )
    return candidate, [entry]


# ---------------------------------------------------------------------------
# Signature / purity.
# ---------------------------------------------------------------------------

def test_validate_returns_two_lists():
    """validate -> (errors, warnings), both lists."""
    candidate, wal_tail = _legal_case()
    result = validate(candidate, wal_tail)
    assert isinstance(result, tuple) and len(result) == 2
    errors, warnings = result
    assert isinstance(errors, list)
    assert isinstance(warnings, list)


def test_validate_is_pure_does_not_mutate_inputs():
    """PURE: validate writes nothing and must not mutate the candidate or wal_tail."""
    candidate, wal_tail = _legal_case()
    import copy
    cand_before = copy.deepcopy(candidate)
    tail_before = copy.deepcopy(wal_tail)
    validate(candidate, wal_tail)
    assert candidate == cand_before, "validate must not mutate the candidate binding"
    assert wal_tail == tail_before, "validate must not mutate the wal_tail"


# ---------------------------------------------------------------------------
# DONE-TEST part 1 — a VALID candidate => EMPTY errors (commit allowed).
# ---------------------------------------------------------------------------

def test_valid_candidate_has_empty_errors():
    """A well-formed LEGAL transition produces NO errors -> the executor may commit.

    Kills the 'everything in errors' mutant (which would put a spurious error here)."""
    candidate, wal_tail = _legal_case()
    errors, warnings = validate(candidate, wal_tail)
    assert errors == [], f"a valid candidate must produce zero errors; got {errors!r}"


@pytest.mark.parametrize(
    "from_state, to_state, event",
    [
        ("planned", "claimed", "claim"),
        ("claimed", "spawning", "spawn_ok"),
        ("spawning", "running", "actor_open"),
        ("running", "blocked", "block"),
        ("running", "failed", "signal_FAILED"),
        ("running", "claimed", "re_adopt"),
        ("dead", "claimed", "necro"),
        ("running", "dead", "reconcile_finds_dead"),
    ],
)
def test_all_legal_transitions_pass(from_state, to_state, event):
    """Every edge in the Inc-0 legality table admits cleanly (no errors)."""
    expected_gen = 4
    gen = expected_gen + 1
    candidate = _candidate(state=to_state, generation=gen)
    entry = _entry(
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_gen,
        generation=gen,
        event=event,
    )
    errors, warnings = validate(candidate, [entry])
    assert errors == [], (
        f"legal {from_state}->{to_state} must admit with no errors; got {errors!r}"
    )


# ---------------------------------------------------------------------------
# DONE-TEST part 2 — an ILLEGAL transition => NON-EMPTY errors (rejected pre-write).
# This is the headline "illegal transition rejected before any write".
# ---------------------------------------------------------------------------

def test_illegal_transition_produces_nonempty_errors():
    """An illegal lifecycle transition is an ERROR — rejected before any write.

    ``done`` is a terminal dead-end (states.is_legal('done','running') is False). The candidate
    claims that transition; validate MUST return a non-empty errors list. Kills the
    'return ([],[]) always' mutant."""
    # sanity: the chosen edge really is illegal per the Inc-0 machine.
    assert states.is_legal("done", "running") is False

    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    entry = _entry(
        from_state="done",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="bogus",
    )
    errors, warnings = validate(candidate, [entry])
    assert errors, (
        "an illegal lifecycle transition (done->running) MUST yield a non-empty errors list "
        "so the executor refuses to commit (DAEMON §4.2)"
    )


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        ("done", "running"),    # terminal dead-end departure
        ("failed", "claimed"),  # failed is a dead-end (only dead re-adopts)
        ("planned", "running"), # must pass through claimed/spawning first
        ("running", "spawning"),# no such edge
        ("blocked", "done"),    # blocked only -> running
    ],
)
def test_each_illegal_edge_is_an_error(from_state, to_state):
    """A spread of illegal edges, each an ERROR (parametrized so a single-edge special-case
    mutant cannot survive)."""
    assert states.is_legal(from_state, to_state) is False
    expected_gen = 2
    gen = expected_gen + 1
    candidate = _candidate(state=to_state, generation=gen)
    entry = _entry(
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_gen,
        generation=gen,
        event="x",
    )
    errors, warnings = validate(candidate, [entry])
    assert errors, f"illegal {from_state}->{to_state} must be rejected as an error"


def test_illegal_transition_writes_nothing_returns_only():
    """validate is the GATE, not the writer: even on an illegal transition it only returns
    the error (purity holds on the error path too)."""
    import copy
    expected_gen = 7
    candidate = _candidate(state="running", generation=expected_gen + 1)
    entry = _entry(
        from_state="done", to_state="running",
        expected_generation=expected_gen, generation=expected_gen + 1, event="bogus",
    )
    wal_tail = [entry]
    cand_before, tail_before = copy.deepcopy(candidate), copy.deepcopy(wal_tail)
    validate(candidate, wal_tail)
    assert candidate == cand_before and wal_tail == tail_before


# ---------------------------------------------------------------------------
# Malformed candidate => ERROR (missing required fields; bad generation step).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing_field", ["node_address", "state", "generation", "owner_token"])
def test_missing_required_binding_field_is_an_error(missing_field):
    """A candidate missing a required binding field is malformed -> ERROR (admission deny)."""
    candidate, wal_tail = _legal_case()
    del candidate[missing_field]
    errors, warnings = validate(candidate, wal_tail)
    assert errors, f"a candidate missing required field {missing_field!r} must be an error"


def test_generation_not_expected_plus_one_is_an_error():
    """The per-node CAS post-condition (§4.2): candidate.generation MUST equal
    expected_generation + 1. A candidate that violates it is malformed -> ERROR.

    (The entry says expected_generation=7 so the only valid candidate generation is 8;
    here the candidate carries 7, the stale value.)"""
    expected_gen = 7
    candidate = _candidate(state="done", generation=expected_gen)   # WRONG: should be 8
    entry = _entry(
        from_state="running", to_state="done",
        expected_generation=expected_gen, generation=expected_gen + 1, event="signal_DONE",
    )
    errors, warnings = validate(candidate, [entry])
    assert errors, (
        "candidate.generation must equal expected_generation+1; a mismatch is a malformed "
        "candidate and must be an error (§4.2 per-node CAS post-condition)"
    )


# ---------------------------------------------------------------------------
# DONE-TEST part 3 — warnings ALLOW: a benign anomaly surfaces as a WARNING,
# errors stay EMPTY (commit still allowed). Kills BOTH:
#   - 'everything in errors' (this benign case would wrongly block), and
#   - 'everything in warnings' (the illegal-transition test above would wrongly allow).
# ---------------------------------------------------------------------------

def test_warning_channel_allows_commit_errors_stay_empty():
    """A LESSER issue must surface as a WARNING and leave errors EMPTY (commit allowed).

    We construct a candidate that is structurally LEGAL and well-formed (so errors MUST be
    empty) yet carries a benign anomaly a per-node admission check may flag. The contract
    we pin: there EXISTS a non-error anomaly that yields (errors==[], warnings!=[]).

    A builder routes *some* lesser anomaly to warnings (the recovered validate() does exactly
    this — e.g. the pipe-joined on_trigger warning at L694). We assert the channel works by
    requiring that a valid candidate carrying an extra/odd-but-harmless attribute still commits
    (errors empty) — and that the warnings list is reachable/usable. To avoid over-pinning the
    exact trigger, this test accepts EITHER:
        (a) the anomaly produces a warning (errors==[], warnings!=[]), OR
        (b) the builder treats it as fully benign (errors==[], warnings==[]),
    but in BOTH cases errors MUST be empty. A dedicated warning-SURFACED trigger is pinned in
    test_a_lesser_anomaly_surfaces_as_a_warning below.
    """
    candidate, wal_tail = _legal_case()
    # a harmless extra field that is NOT part of the schema and is NOT a transition violation
    candidate["unrecognized_bookkeeping_hint"] = "ignored"
    errors, warnings = validate(candidate, wal_tail)
    assert errors == [], (
        "a benign anomaly on an otherwise-legal candidate must NOT block commit — errors "
        "stay empty (kills the 'everything in errors' mutant)"
    )


def test_warnings_do_not_block_commit_on_the_valid_case():
    """Reinforce warnings-allow: on the valid case, whatever warnings exist, errors are empty.

    This is the asymmetry that distinguishes errors (block) from warnings (allow): the executor
    keys ONLY on errors. If a mutant routed the legality verdict into warnings, the illegal-
    transition test fails; if a mutant routed everything into errors, THIS test fails because a
    valid commit would be blocked."""
    candidate, wal_tail = _legal_case()
    errors, warnings = validate(candidate, wal_tail)
    assert errors == [], "the valid case must always have empty errors regardless of warnings"


def test_a_lesser_anomaly_surfaces_as_a_warning():
    """A lesser anomaly is SURFACED as a WARNING (non-empty warnings) while errors stay EMPTY.

    This directly exercises the warnings CHANNEL: the spec mandates "lesser issues are WARNINGS
    (allow)". We pin the canonical §3.6 lesser case — an ESCALATED stamp, which by the §3.6
    asymmetry rule sets terminal_signal=ESCALATED but does NOT change lifecycle state and does
    NOT collapse. The WAL entry therefore carries from_state==to_state==running (a NO-OP, not a
    legal forward edge): a faithful per-node admission check must NOT reject it as an illegal
    transition (ESCALATED legitimately holds its slot) yet SHOULD note the no-op signal-stamp as
    a warning rather than silently admit a non-transition.

    FORK (disclosed): the spec fixes "lesser issues -> warnings" but does not enumerate the exact
    lesser-issue set for per-node v1. This test pins the ESCALATED no-op as the warning trigger.
    A builder that classifies a different anomaly as its canonical warning may need this trigger
    adjusted — but the load-bearing invariant (errors EMPTY here, warnings REACHABLE and used,
    so 'everything in warnings' and 'everything in errors' are both impossible) is the point.
    """
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["terminal_signal"] = "ESCALATED"   # §3.6: signal SET, state STAYS running
    entry = _entry(
        from_state="running",
        to_state="running",          # no-op: ESCALATED does not change lifecycle state
        expected_generation=expected_gen,
        generation=gen,
        event="signal_ESCALATED",
    )
    errors, warnings = validate(candidate, [entry])
    assert errors == [], (
        "an ESCALATED stamp must NOT be rejected as an illegal transition — it legitimately "
        f"holds its slot (running stays running); got errors={errors!r}"
    )
    assert warnings, (
        "the ESCALATED no-op (signal stamped, no state change) is a lesser issue that must be "
        "SURFACED as a warning — this proves the warnings channel allows-but-flags (kills the "
        "'everything in errors' AND the 'never warns' mutants)"
    )


def test_gate_candidate_submission_self_loop_is_admitted_with_warning():
    """A gated producer submits a candidate while keeping context for PASS/BOUNCE. That is a
    deliberate running->running self-loop, analogous to ESCALATED but keyed by gate_state instead
    of terminal_signal. It must be admitted while still surfacing as a warning."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "candidate_submitted"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_candidate_submitted",
        binding_delta={"gate_state": "candidate_submitted"},
    )
    errors, warnings = validate(candidate, [entry])
    assert errors == [], (
        "a gate candidate submission must keep the producer running without being rejected as an "
        f"illegal no-op; got errors={errors!r}"
    )
    assert warnings and "gate candidate" in " ".join(warnings), (
        "the gate self-loop should be visible as a warning, not silently admitted"
    )


def test_gate_candidate_submission_can_retry_after_gate_failed():
    """A repaired gate can explicitly move from gate_failed back to candidate_submitted."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "candidate_submitted"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_candidate_submitted",
        binding_delta={
            "gate_state_before": "gate_failed",
            "gate_state": "candidate_submitted",
            "gate_retried_at": "2026-06-15T00:00:00+00:00",
        },
    )
    errors, warnings = validate(candidate, [entry])
    assert errors == [], errors
    assert warnings and "gate candidate" in " ".join(warnings)


def test_stale_gate_candidate_state_does_not_admit_unrelated_self_loop():
    """The gate no-op admission is event-scoped. A binding that already carries
    gate_state=candidate_submitted must not make an unrelated self-loop legal."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "candidate_submitted"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="unrelated_noop",
    )
    errors, warnings = validate(candidate, [entry])
    assert errors, "stale gate_state must not admit an unrelated running->running no-op"
    assert warnings == []


def test_gate_bounce_self_loop_is_admitted_with_warning():
    """A gate BOUNCE returns typed defects while preserving the producer's live context."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "gate_bounced"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_bounced",
        binding_delta={"gate_state_before": "candidate_submitted", "gate_state": "gate_bounced"},
    )
    errors, warnings = validate(candidate, [entry])
    assert errors == [], (
        "a gate bounce must keep the producer running without being rejected as an illegal no-op"
    )
    assert warnings and "gate bounce" in " ".join(warnings), (
        "the gate bounce self-loop should be visible as a warning"
    )


def test_gate_failed_self_loop_is_admitted_with_warning():
    """A gate substrate failure is parent-visible while preserving producer context."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "gate_failed"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_failed",
        binding_delta={"gate_state_before": "candidate_submitted", "gate_state": "gate_failed"},
    )
    errors, warnings = validate(candidate, [entry])
    assert errors == [], (
        "a gate failure must preserve the producer context without being rejected as an illegal no-op"
    )
    assert warnings and "gate failure" in " ".join(warnings), (
        "the gate_failed self-loop should be visible as a warning"
    )


def test_unknown_gate_state_is_rejected():
    """Gate state is a closed enum, not an arbitrary binding string."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "reviewish"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_candidate_submitted",
        binding_delta={"gate_state": "reviewish"},
    )
    errors, _warnings = validate(candidate, [entry])
    assert any("illegal gate_state" in e for e in errors)


def test_gate_event_must_land_on_legal_gate_state():
    """A gate event cannot write a plausible but wrong gate_state."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "gate_passed"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_bounced",
        binding_delta={"gate_state": "gate_passed"},
    )
    errors, _warnings = validate(candidate, [entry])
    assert any("illegal gate transition" in e for e in errors)


def test_gate_bounce_can_follow_candidate_submission_prior_bounce_or_parent_return():
    """Bounce loops are explicit, including the parent-return edge from escalation."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "gate_bounced"
    for previous in ("candidate_submitted", "gate_bounced", "gate_escalated"):
        entry = _entry(
            from_state="running",
            to_state="running",
            expected_generation=expected_gen,
            generation=gen,
            event="gate_bounced",
            binding_delta={"gate_state_before": previous, "gate_state": "gate_bounced"},
        )
        errors, warnings = validate(candidate, [entry])
        assert not errors, (previous, errors)
        assert warnings


def test_gate_passed_requires_lifecycle_done():
    """The only parent-forwarding gate state is tied to finalizing the producer."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="running", generation=gen)
    candidate["gate_state"] = "gate_passed"
    entry = _entry(
        from_state="running",
        to_state="running",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_passed",
        binding_delta={"gate_state_before": "candidate_submitted", "gate_state": "gate_passed"},
    )
    errors, _warnings = validate(candidate, [entry])
    assert any("gate_passed" in e and "done" in e for e in errors)


def test_gate_passed_can_resolve_prior_gate_escalation_when_done():
    """A parent ACCEPT of an escalated gate finalizes through the normal gate_passed state."""
    expected_gen = 7
    gen = expected_gen + 1
    candidate = _candidate(state="done", generation=gen)
    candidate["gate_state"] = "gate_passed"
    entry = _entry(
        from_state="running",
        to_state="done",
        expected_generation=expected_gen,
        generation=gen,
        event="gate_passed",
        binding_delta={"gate_state_before": "gate_escalated", "gate_state": "gate_passed"},
    )
    errors, _warnings = validate(candidate, [entry])
    assert not errors
