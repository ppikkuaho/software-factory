"""Increment 4 — TERMINAL_VOCAB acceptance (the §3.6 normative mapping table).

FROZEN acceptance. Asserts that ``harnessd.states.TERMINAL_VOCAB`` encodes the ONE
normative terminal-vocabulary mapping table (DAEMON §3.6 / IMPLEMENTATION-PLAN §2.3)
that translates among the THREE deliberately-distinct layers:

    agent-emitted tag  ->  terminal_signal (binding)  ->  run-ledger event  ->  lifecycle state  ->  collapsed?

The two load-bearing subtleties a builder MUST encode (and these tests pin):

  1. ESCALATED is ASYMMETRIC. ``terminal_signal`` is SET (``ESCALATED``) but the lifecycle
     ``state`` STAYS ``running`` and the node is NOT collapsed. A mutant that maps ESCALATED
     to ``done``/collapsed-True (the naive "terminal_signal!=null => collapse" assumption) is
     caught here.

  2. The spelling split is DELIBERATE, not an accident. The binding value is SCREAMING
     (``DIED_INFRA``), the run-ledger event is snake (``died_infrastructure``), the lifecycle
     state is lowercase (``failed``). A mutant that "unifies" the three layers by renaming is
     caught here (the three must be DISTINCT strings).

These tests are interface-faithful: they index TERMINAL_VOCAB by the §3.6 *row* and read its
mapped layers, without assuming the precise nesting shape beyond "a row exposes its four mapped
layers." A helper normalizes the row so the suite tolerates either a dict-of-fields or a
named-tuple/object representation, but the *values* are pinned exactly to §3.6.
"""

import pytest

import harnessd.states as states


# ---------------------------------------------------------------------------
# Shape-tolerant row reader. The §3.6 row maps four (sometimes five) layers; a
# builder may represent each row as a dict, a namedtuple, or a small object. We
# read by attribute/key name so the spelling/asymmetry assertions below pin the
# VALUES (the load-bearing part), not an incidental container choice.
# ---------------------------------------------------------------------------

# Canonical field names for the §3.6 columns, with the aliases a faithful
# builder might plausibly choose. The test fails loudly if a row exposes NONE
# of the aliases for a column it asserts on.
_TERMINAL_SIGNAL_KEYS = ("terminal_signal", "signal", "binding_signal")
_EVENT_KEYS = ("event", "run_ledger_event", "ledger_event")
_STATE_KEYS = ("state", "lifecycle_state", "resulting_state", "to_state")
_COLLAPSED_KEYS = ("collapsed", "collapse", "is_collapsed", "node_collapsed")


def _get(row, keys):
    """Return (found, value) for the first present alias among ``keys`` in ``row``.

    ``row`` may be a mapping (dict) or an object with attributes."""
    if isinstance(row, dict):
        for k in keys:
            if k in row:
                return True, row[k]
        return False, None
    for k in keys:
        if hasattr(row, k):
            return True, getattr(row, k)
    return False, None


def _terminal_signal(row):
    found, val = _get(row, _TERMINAL_SIGNAL_KEYS)
    assert found, f"§3.6 row {row!r} exposes no terminal_signal field (tried {_TERMINAL_SIGNAL_KEYS})"
    return val


def _event(row):
    found, val = _get(row, _EVENT_KEYS)
    assert found, f"§3.6 row {row!r} exposes no run-ledger event field (tried {_EVENT_KEYS})"
    return val


def _state(row):
    found, val = _get(row, _STATE_KEYS)
    assert found, f"§3.6 row {row!r} exposes no lifecycle state field (tried {_STATE_KEYS})"
    return val


def _collapsed(row):
    found, val = _get(row, _COLLAPSED_KEYS)
    assert found, f"§3.6 row {row!r} exposes no collapsed field (tried {_COLLAPSED_KEYS})"
    return val


def _row_by_event(event_name):
    """Find the §3.6 row whose run-ledger event == ``event_name``.

    TERMINAL_VOCAB may be keyed by agent-tag, by terminal_signal, or by event; the
    run-ledger event is unique across all eight rows (DONE/FAILED share an agent tag
    with their coordinator variants only via DISTINCT events), so locate by event."""
    vocab = states.TERMINAL_VOCAB
    rows = vocab.values() if isinstance(vocab, dict) else list(vocab)
    matches = [r for r in rows if _event(r) == event_name]
    assert len(matches) == 1, (
        f"expected exactly one §3.6 row with run-ledger event {event_name!r}, "
        f"found {len(matches)}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# Existence / Inc-0 invariants stay green.
# ---------------------------------------------------------------------------

def test_terminal_vocab_exists_and_is_a_mapping():
    """TERMINAL_VOCAB is present on states.py (deferred from Inc-0, added in Inc-4)."""
    assert hasattr(states, "TERMINAL_VOCAB"), "states.TERMINAL_VOCAB must exist (§2.3 / §3.6)"
    vocab = states.TERMINAL_VOCAB
    assert isinstance(vocab, dict), "TERMINAL_VOCAB is the §3.6 mapping (a dict keyed by row)"
    assert len(vocab) >= 8, (
        "the §3.6 table has eight normative rows (DONE, FAILED, ESCALATED, DIED_INFRA, "
        "DIED_METHODOLOGY, FENCED, coordinator_died, coordinator_completed)"
    )


def test_inc0_state_machine_surface_unchanged():
    """Adding TERMINAL_VOCAB must NOT disturb the Inc-0 frozen interface."""
    assert states.is_terminal("done") is True
    assert states.is_terminal("running") is False
    assert states.is_legal("running", "done") is True
    assert states.is_legal("done", "running") is False
    assert states.LIVENESS_STATES == ("working", "waiting", "idle", "dead")
    # running's allowed targets stay exactly the Inc-0 set.
    assert states.ALLOWED_TRANSITIONS["running"] == frozenset(
        {"blocked", "done", "failed", "claimed", "dead"}
    )


# ---------------------------------------------------------------------------
# Per-row value pins — the exact §3.6 translation among the three layers.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "agent_tag, terminal_signal, event, state, collapsed",
    [
        # agent-emitted-tag rows (the strict 3-set):
        ("DONE", "DONE", "signal_DONE", "done", True),
        ("FAILED", "FAILED", "signal_FAILED", "failed", True),
        ("ESCALATED", "ESCALATED", "signal_ESCALATED", "running", False),
        # daemon-stamped death classes:
        (None, "DIED_INFRA", "died_infrastructure", "failed", None),
        (None, "DIED_METHODOLOGY", "died_methodology", "failed", None),
    ],
)
def test_terminal_vocab_row_translation(agent_tag, terminal_signal, event, state, collapsed):
    """Each §3.6 row translates agent-tag -> signal -> event -> state EXACTLY.

    ``collapsed=None`` (the died_* rows: "per ② recovery policy") means we do not pin the
    collapsed cell — those rows resolve to ``failed`` and are routed by ②'s policy, so the
    table need only carry the signal/event/state translation for them.
    """
    row = _row_by_event(event)
    assert _terminal_signal(row) == terminal_signal, (
        f"{event}: terminal_signal must be {terminal_signal!r} (binding layer)"
    )
    assert _event(row) == event, f"{event}: run-ledger event must be {event!r}"
    assert _state(row) == state, f"{event}: lifecycle state must be {state!r}"
    if collapsed is not None:
        assert _collapsed(row) == collapsed, (
            f"{event}: collapsed must be {collapsed!r}"
        )


def test_fenced_row_leaves_state_unchanged_and_does_not_collapse():
    """The FENCED row: terminal_signal=FENCED, event=stale_return_ignored, NO collapse.

    The live owner is unaffected (a stale actor's leftover return is ignored); §3.6 marks
    its lifecycle cell "(unchanged)" and collapsed = no. We pin signal+event+not-collapsed;
    we do NOT pin a concrete lifecycle state (the row's state cell is "unchanged")."""
    row = _row_by_event("stale_return_ignored")
    assert _terminal_signal(row) == "FENCED"
    # FENCED must NOT collapse the node (live owner unaffected).
    assert _collapsed(row) is False, "FENCED is non-destructive de-auth: never collapses the live node"


def test_coordinator_rows_present_and_distinct():
    """The two daemon-stamped coordinator rows: coordinator_died -> dead (not collapsed);
    coordinator_completed -> done (collapsed)."""
    died = _row_by_event("coordinator_died")
    completed = _row_by_event("coordinator_completed")
    assert _state(died) == "dead", "coordinator_died resolves the node to lifecycle 'dead' (§5.4)"
    assert _collapsed(died) is False, "a dead coordinator is recovered-as-orphan, NOT collapsed (§5.4)"
    assert _state(completed) == "done", "coordinator_completed resolves the node to 'done'"
    assert _terminal_signal(completed) == "DONE", "coordinator_completed stamps terminal_signal DONE"


# ---------------------------------------------------------------------------
# LOAD-BEARING #1 — the ESCALATED asymmetry (mutant: ESCALATED -> done/collapsed).
# ---------------------------------------------------------------------------

def test_escalated_is_asymmetric_state_stays_running_not_collapsed():
    """ESCALATED: terminal_signal SET, but lifecycle state STAYS 'running' and NOT collapsed.

    This is the headline asymmetry. A mutant that maps ESCALATED to state 'done' / collapsed True
    (the naive 'terminal_signal != null => collapse' assumption the spec explicitly forbids) is
    caught here. Gate collapse on state ∈ {done,failed,dead}, NEVER on (terminal_signal != null)."""
    row = _row_by_event("signal_ESCALATED")
    assert _terminal_signal(row) == "ESCALATED", "ESCALATED DOES set a terminal_signal..."
    assert _state(row) == "running", "...yet the lifecycle state STAYS 'running' (asymmetric)"
    assert _state(row) not in states.TERMINAL_STATES, (
        "ESCALATED's resulting state must NOT be terminal — the node keeps its slot and waits"
    )
    assert _collapsed(row) is False, "ESCALATED is NOT collapsed: it holds context and waits for the answer"


def test_escalated_state_is_not_a_terminal_done_or_failed():
    """Explicit negative pin: a wrong ESCALATED->done/failed mapping fails (kills the
    'collapse-on-signal' mutant even if it preserves a truthy terminal_signal)."""
    row = _row_by_event("signal_ESCALATED")
    assert _state(row) != "done"
    assert _state(row) != "failed"
    assert _state(row) != "dead"


# ---------------------------------------------------------------------------
# LOAD-BEARING #2 — the deliberate spelling split (mutant: unify the three layers).
# ---------------------------------------------------------------------------

def test_died_infra_three_layers_are_three_distinct_strings():
    """DIED_INFRA: the binding value, run-ledger event, and lifecycle state are THREE
    DISTINCT strings. A mutant that unifies them by renaming (all three == 'DIED_INFRA',
    or all three == 'died_infrastructure') is caught here."""
    row = _row_by_event("died_infrastructure")
    signal = _terminal_signal(row)   # SCREAMING binding value
    event = _event(row)              # snake run-ledger event
    state = _state(row)              # lowercase lifecycle state

    assert signal == "DIED_INFRA", "binding layer is SCREAMING DIED_INFRA"
    assert event == "died_infrastructure", "run-ledger layer is snake died_infrastructure"
    assert state == "failed", "lifecycle layer is lowercase failed"

    # The load-bearing part: the three layers are genuinely DISTINCT strings.
    assert len({signal, event, state}) == 3, (
        "the spelling split is deliberate — binding/event/state for DIED_INFRA must be "
        "three distinct strings; do NOT unify them by renaming"
    )
    # And the case-discipline is exactly screaming / snake / lowercase.
    assert signal.isupper(), "binding value is SCREAMING"
    assert event.islower() and "_" in event, "run-ledger event is snake-cased lowercase"
    assert state.islower() and "_" not in state, "lifecycle state is a single lowercase word"


def test_died_methodology_distinct_from_died_infra_across_all_layers():
    """infra-vs-methodology are DISTINCT terminal classes (F-017): the binding value and
    the run-ledger event differ, even though both resolve to lifecycle 'failed'. A mutant
    that collapses the two death classes into one is caught here."""
    infra = _row_by_event("died_infrastructure")
    method = _row_by_event("died_methodology")
    assert _terminal_signal(infra) != _terminal_signal(method), (
        "DIED_INFRA and DIED_METHODOLOGY must be distinct binding values (F-017)"
    )
    assert _event(infra) != _event(method), (
        "died_infrastructure and died_methodology must be distinct run-ledger events (F-017)"
    )
    # Both still resolve to the same lifecycle terminal — that part is shared by design.
    assert _state(infra) == "failed"
    assert _state(method) == "failed"


def test_every_lifecycle_state_in_vocab_is_a_known_state():
    """Cross-check: every lifecycle state the §3.6 table resolves to is a KNOWN_STATES
    value (the table cannot invent a lifecycle state outside the Inc-0 machine)."""
    vocab = states.TERMINAL_VOCAB
    rows = vocab.values() if isinstance(vocab, dict) else list(vocab)
    for row in rows:
        found, st = _get(row, _STATE_KEYS)
        if found and st is not None:
            assert st in states.KNOWN_STATES, (
                f"§3.6 resolved lifecycle state {st!r} is not a KNOWN_STATES value"
            )
