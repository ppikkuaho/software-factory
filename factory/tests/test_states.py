"""Increment 0 Done-test (clause 2 — the headline): states.ALLOWED_TRANSITIONS
round-trips the AUTHORITATIVE legality table, INCLUDING the rollback + re-adopt edges.

Authoritative source: IMPLEMENTATION-PLAN.md §2.3 (frozen states.py interface) +
DAEMON §3.3 generic per-node lifecycle state machine, transcribed below as the single
source of truth for this suite. A states.py that forgot a re-adopt edge, dropped a
rollback edge, or admitted an illegal edge MUST fail one of these tests.

Frozen interface under test (§2.3):
    ALLOWED_TRANSITIONS: dict[str, set[str]]
    def is_terminal(state: str) -> bool
    def is_legal(from_state: str, to_state: str) -> bool

These tests fail RED until harnessd/states.py exists and is correct.
"""

import pytest

# Direct import (NOT importorskip): until the builder creates harnessd/states.py this is a
# collection-time ImportError, which pytest reports as a RED error for every test in the file.
# That hard failure is the intended Increment-0 RED signal for the headline Done-test — a skip
# would wrongly read as "satisfied".
import harnessd.states as states


# --------------------------------------------------------------------------------------
# AUTHORITATIVE legality table (DAEMON §3.3). This is the frozen spec, transcribed once.
# Every legal edge appears here exactly. Illegal edges are everything NOT here.
# --------------------------------------------------------------------------------------

ALL_STATES = {
    "planned",
    "claimed",
    "spawning",
    "running",
    "blocked",
    "done",
    "failed",
    "dead",
}
TERMINAL_STATES = {"done", "failed", "dead"}
NON_TERMINAL_STATES = ALL_STATES - TERMINAL_STATES  # planned,claimed,spawning,running,blocked

# Forward / primary lifecycle edges.
PRIMARY_EDGES = {
    ("planned", "claimed"),    # claim
    ("claimed", "spawning"),   # spawn-ok
    ("spawning", "running"),   # actor-open
    ("running", "blocked"),    # block
    ("blocked", "running"),    # unblock
    ("running", "done"),       # DONE
    ("running", "failed"),     # FAILED/DIED
}

# Rollback edges — FIRST-CLASS. Without them the spawn chokepoint leaks an
# un-reclaimable claimed slot.
ROLLBACK_EDGES = {
    ("claimed", "planned"),    # admission-deny / E32-pin-fail rollback (§6.1)
    ("spawning", "planned"),   # actor-open-fails rollback (§6.1)
    ("spawning", "failed"),    # unrecoverable-spawn-error — give up the slot, not retry
}

# Re-adopt edges — FIRST-CLASS. Without them resume/necro is un-buildable.
READOPT_EDGES = {
    ("running", "claimed"),    # re-adopt a LIVE address (expected_state=running) §6.4
    ("dead", "claimed"),       # re-adopt/necro a DEAD address (expected_state=dead) §6.4/§5
}

# Reconcile-driven: every non-terminal -> dead (reconcile-finds-dead).
RECONCILE_DEAD_EDGES = {(s, "dead") for s in NON_TERMINAL_STATES}

# Reconcile-driven §3.6 death classes: every non-terminal -> failed (the daemon-stamped
# died_* classes resolve to lifecycle state `failed`, DAEMON §3.6; review reconcile-1).
RECONCILE_FAILED_EDGES = {(s, "failed") for s in NON_TERMINAL_STATES}

# The complete legal edge set.
EXPECTED_LEGAL_EDGES = (
    PRIMARY_EDGES | ROLLBACK_EDGES | READOPT_EDGES | RECONCILE_DEAD_EDGES | RECONCILE_FAILED_EDGES
)

# A representative battery of ILLEGAL edges that MUST be rejected.
ILLEGAL_EDGES = {
    ("planned", "running"),    # cannot skip claim+spawn
    ("planned", "spawning"),   # cannot skip claim
    ("claimed", "running"),    # cannot skip spawning
    ("done", "running"),       # terminal has no outgoing edge
    ("failed", "running"),     # terminal has no outgoing edge
    ("dead", "running"),       # dead re-adopts to CLAIMED, never straight to running
    ("done", "claimed"),       # done is terminal — no re-adopt off done
    ("failed", "claimed"),     # failed is terminal — no re-adopt off failed
    ("blocked", "done"),       # must unblock to running first
    ("blocked", "claimed"),    # not a re-adopt source
    ("spawning", "blocked"),   # blocked is only reachable from running
    ("planned", "done"),       # cannot reach terminal directly from planned
    ("running", "spawning"),   # no backward edge into spawning
}


# --------------------------------------------------------------------------------------
# Helpers that read the static table tolerantly (set OR sequence values both accepted).
# --------------------------------------------------------------------------------------

def _targets(from_state):
    table = states.ALLOWED_TRANSITIONS
    assert from_state in table, f"state {from_state!r} missing from ALLOWED_TRANSITIONS"
    return set(table[from_state])


def _edges_in_table():
    edges = set()
    for src, targets in states.ALLOWED_TRANSITIONS.items():
        for dst in targets:
            edges.add((src, dst))
    return edges


# --------------------------------------------------------------------------------------
# Structural: the table covers exactly the known states.
# --------------------------------------------------------------------------------------

def test_allowed_transitions_exists_and_is_mapping():
    assert hasattr(states, "ALLOWED_TRANSITIONS"), "states.ALLOWED_TRANSITIONS must exist"
    assert isinstance(states.ALLOWED_TRANSITIONS, dict)


def test_all_known_states_are_keys():
    """Every lifecycle state (incl. terminals) is a key in the table."""
    assert set(states.ALLOWED_TRANSITIONS.keys()) == ALL_STATES


def test_no_target_is_an_unknown_state():
    """No transition points at a state outside the known 8."""
    for src, targets in states.ALLOWED_TRANSITIONS.items():
        unknown = set(targets) - ALL_STATES
        assert not unknown, f"{src!r} -> unknown target(s) {unknown}"


# --------------------------------------------------------------------------------------
# THE round-trip: ALLOWED_TRANSITIONS == EXACTLY the authoritative legal edge set.
# This single assertion catches both a MISSING legal edge and an EXTRA illegal edge.
# --------------------------------------------------------------------------------------

def test_allowed_transitions_round_trips_legality_table_exactly():
    actual = _edges_in_table()
    missing = EXPECTED_LEGAL_EDGES - actual
    extra = actual - EXPECTED_LEGAL_EDGES
    assert not missing, f"MISSING legal edges (table forgot them): {sorted(missing)}"
    assert not extra, f"EXTRA illegal edges (table over-permits): {sorted(extra)}"


@pytest.mark.parametrize("edge", sorted(EXPECTED_LEGAL_EDGES))
def test_each_legal_edge_present(edge):
    """Each individual legal edge is present (per-edge granularity for clear failures)."""
    src, dst = edge
    assert dst in _targets(src), f"legal edge {src}->{dst} missing from ALLOWED_TRANSITIONS"


# --------------------------------------------------------------------------------------
# ROLLBACK edges — named, load-bearing (the spawn chokepoint leak guard).
# --------------------------------------------------------------------------------------

def test_rollback_edge_claimed_to_planned_present():
    """claimed->planned (admission-deny / pin-fail release) — without it a claimed slot
    becomes un-reclaimable."""
    assert "planned" in _targets("claimed")


def test_rollback_edge_spawning_to_planned_present():
    """spawning->planned (actor-open-fails rollback)."""
    assert "planned" in _targets("spawning")


def test_rollback_edge_spawning_to_failed_present():
    """spawning->failed (unrecoverable-spawn-error — give up the slot)."""
    assert "failed" in _targets("spawning")


def test_all_rollback_edges_present_as_group():
    actual = _edges_in_table()
    missing = ROLLBACK_EDGES - actual
    assert not missing, f"rollback edges missing: {sorted(missing)}"


# --------------------------------------------------------------------------------------
# RE-ADOPT edges — named, load-bearing (resume/necro un-buildable without them).
# --------------------------------------------------------------------------------------

def test_readopt_edge_running_to_claimed_present():
    """running->claimed (re-adopt a LIVE address, expected_state=running; §6.4)."""
    assert "claimed" in _targets("running")


def test_readopt_edge_dead_to_claimed_present():
    """dead->claimed (re-adopt/necro a DEAD address, expected_state=dead; §6.4/§5)."""
    assert "claimed" in _targets("dead")


def test_all_readopt_edges_present_as_group():
    actual = _edges_in_table()
    missing = READOPT_EDGES - actual
    assert not missing, f"re-adopt edges missing: {sorted(missing)}"


# --------------------------------------------------------------------------------------
# Reconcile-driven {non-terminal} -> dead.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("src", sorted(NON_TERMINAL_STATES))
def test_every_nonterminal_can_go_dead(src):
    """reconcile-finds-dead: any non-terminal state -> dead is legal."""
    assert "dead" in _targets(src), f"{src}->dead (reconcile) missing"


# --------------------------------------------------------------------------------------
# TERMINAL states have NO outgoing edges — for the FORWARD/collapse machine.
#
# Nuance (the table is authoritative over the prose summary): done and failed are true
# dead-ends with ZERO outgoing edges. `dead` is also terminal, yet the AUTHORITATIVE table
# lists `dead -> claimed` as a FIRST-CLASS re-adopt/necro departure (§6.4/§5). So "no
# outgoing edges" is asserted strictly for {done, failed}; `dead`'s ONLY legal departure is
# the single re-adopt edge to claimed. A test that forbade dead->claimed would force the
# builder to DROP a first-class re-adopt edge — the exact failure the spec warns against.
# --------------------------------------------------------------------------------------

FORWARD_DEAD_END_STATES = {"done", "failed"}  # true zero-outgoing terminals


@pytest.mark.parametrize("term", sorted(FORWARD_DEAD_END_STATES))
def test_forward_terminal_states_have_no_outgoing_edges(term):
    assert _targets(term) == set(), f"terminal state {term!r} must have NO outgoing edges, got {_targets(term)}"


def test_done_is_terminal_dead_end():
    assert _targets("done") == set()


def test_failed_is_terminal_dead_end():
    assert _targets("failed") == set()


def test_dead_only_outgoing_is_readopt_to_claimed():
    """dead is terminal for the FORWARD machine, but the re-adopt/necro edge dead->claimed
    is the SOLE legal departure — and it goes to claimed, never anywhere else. This is the
    reconciliation of 'terminals have no outgoing edges' with 'dead->claimed is first-class':
    dead has no FORWARD edge, only the single backward re-adopt."""
    assert _targets("dead") == {"claimed"}


# --------------------------------------------------------------------------------------
# ILLEGAL edges are rejected by both the static table AND the legality predicate.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("edge", sorted(ILLEGAL_EDGES))
def test_illegal_edge_not_in_table(edge):
    src, dst = edge
    assert dst not in _targets(src), f"illegal edge {src}->{dst} must NOT be in ALLOWED_TRANSITIONS"


# --------------------------------------------------------------------------------------
# is_legal(from, to) predicate (§2.3) agrees with the table for ALL edges, legal & illegal.
# --------------------------------------------------------------------------------------

def test_is_legal_exists_and_callable():
    assert hasattr(states, "is_legal") and callable(states.is_legal)


@pytest.mark.parametrize("edge", sorted(EXPECTED_LEGAL_EDGES))
def test_is_legal_true_for_every_legal_edge(edge):
    src, dst = edge
    assert states.is_legal(src, dst) is True, f"is_legal({src!r},{dst!r}) should be True"


@pytest.mark.parametrize("edge", sorted(ILLEGAL_EDGES))
def test_is_legal_false_for_every_illegal_edge(edge):
    src, dst = edge
    assert states.is_legal(src, dst) is False, f"is_legal({src!r},{dst!r}) should be False"


def test_is_legal_rejects_self_loops_for_terminals():
    """A terminal state cannot transition to itself."""
    for term in sorted(TERMINAL_STATES):
        assert states.is_legal(term, term) is False, f"{term}->{term} self-loop must be illegal"


# Specific named illegal-edge guards (the most diagnostic regressions, called out by the spec).
def test_planned_to_running_is_illegal():
    assert states.is_legal("planned", "running") is False


def test_done_to_running_is_illegal():
    assert states.is_legal("done", "running") is False


def test_claimed_to_running_is_illegal():
    assert states.is_legal("claimed", "running") is False


# --------------------------------------------------------------------------------------
# is_terminal(state) predicate (§2.3).
# --------------------------------------------------------------------------------------

def test_is_terminal_exists_and_callable():
    assert hasattr(states, "is_terminal") and callable(states.is_terminal)


@pytest.mark.parametrize("term", sorted(TERMINAL_STATES))
def test_is_terminal_true_for_terminals(term):
    assert states.is_terminal(term) is True


@pytest.mark.parametrize("nonterm", sorted(NON_TERMINAL_STATES))
def test_is_terminal_false_for_nonterminals(nonterm):
    assert states.is_terminal(nonterm) is False


def test_is_terminal_set_matches_outgoing_edge_emptiness_except_dead():
    """Consistency: done/failed have no outgoing edges; dead is terminal yet keeps its
    sole re-adopt departure. The terminal SET is {done, failed, dead} regardless."""
    terminals_by_predicate = {s for s in ALL_STATES if states.is_terminal(s)}
    assert terminals_by_predicate == TERMINAL_STATES
