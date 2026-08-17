"""Increment 4 — load-bearing STRENGTHENING (mutation-review gate).

Gaps the review flagged:
  1. (MEDIUM correctness) validate admitted ANY from==to no-op as legal; only the §3.6 ESCALATED
     slot-hold (running->running with terminal_signal=ESCALATED) is legitimate. A DONE no-op or an
     unmotivated self-loop must ERROR. (Impl tightened; these lock it.)
  2. No test pinned warnings==[] on a clean candidate -> a spurious always-on-warning mutant survived.
  3. No test asserted WHICH error fired -> a wrong-reason mutant survived (assert the illegal-edge message).
  4. TERMINAL_VOCAB FENCED state cell (None) and coordinator_completed collapsed cell (True) were unpinned.
"""

import harnessd.ledger as ledger
import harnessd.states as states
import harnessd.validate as validate


def _entry(from_state, to_state, expected_generation=0):
    return ledger.build_wal_record(
        node_address="proj/x#exec", event="transition",
        from_state=from_state, to_state=to_state,
        expected_generation=expected_generation, generation=expected_generation + 1,
        lease_epoch=1, owner_token="proj/x#exec:sa:uuid:1",
        binding_delta={"state": to_state}, summary="s", artifacts=[], seq=1,
    )


def _candidate(state, generation=1, terminal_signal=None):
    c = {"node_address": "proj/x#exec", "state": state, "generation": generation,
         "owner_token": "proj/x#exec:sa:uuid:1"}
    if terminal_signal is not None:
        c["terminal_signal"] = terminal_signal
    return c


# --- validate: the tightened no-op handling -----------------------------------------------------

def test_validate_rejects_done_noop():
    """A running->running no-op carrying terminal_signal=DONE is NOT the ESCALATED slot-hold -> ERROR."""
    errors, _ = validate.validate(_candidate("running", terminal_signal="DONE"), [_entry("running", "running")])
    assert errors, "a DONE self-loop must block commit (only ESCALATED running->running is admitted)"


def test_validate_rejects_unmotivated_noop():
    """A running->running no-op with NO terminal_signal is an unmotivated self-loop -> ERROR."""
    errors, _ = validate.validate(_candidate("running"), [_entry("running", "running")])
    assert errors, "an unmotivated self-loop must block commit"


def test_validate_admits_escalated_noop_with_warning():
    """Control: running->running + ESCALATED is admitted (no error) and flagged (warning)."""
    errors, warnings = validate.validate(_candidate("running", terminal_signal="ESCALATED"), [_entry("running", "running")])
    assert errors == [], "the ESCALATED slot-hold must NOT error"
    assert warnings, "the ESCALATED slot-hold should surface a benign warning"


def test_validate_clean_candidate_has_no_warnings():
    """A clean, legal, well-formed candidate -> NO errors AND NO warnings (kills a spurious-warning mutant)."""
    errors, warnings = validate.validate(_candidate("claimed"), [_entry("planned", "claimed")])
    assert errors == [], f"clean candidate must not error, got {errors}"
    assert warnings == [], f"clean candidate must not warn, got {warnings}"


def test_validate_illegal_transition_error_names_the_transition():
    """Error reason specificity: the illegal-edge error message identifies the offending transition."""
    errors, _ = validate.validate(_candidate("running"), [_entry("planned", "running")])  # planned->running illegal
    assert any("planned" in e and "running" in e and "illegal" in e.lower() for e in errors), \
        f"the illegal-transition error must name the transition, got {errors}"


# --- TERMINAL_VOCAB: pin the previously-unpinned cells -------------------------------------------

def test_fenced_row_state_is_none():
    """FENCED (stale_return_ignored) leaves the live owner's state UNCHANGED -> state cell is None."""
    assert states.TERMINAL_VOCAB["stale_return_ignored"].state is None


def test_coordinator_completed_collapses():
    """A completed coordinator IS collapsed (collapsed=True), distinct from coordinator_died."""
    assert states.TERMINAL_VOCAB["coordinator_completed"].collapsed is True
    assert states.TERMINAL_VOCAB["coordinator_died"].collapsed is False
