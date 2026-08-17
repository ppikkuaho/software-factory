"""Pure per-node admission disciplines.

``validate(candidate, wal_tail)`` is the strict validate-before-commit
lifecycle gate. ``validate_committed_snapshot(candidate, node_wal)`` is the
whole-ledger read projection: it validates the already-committed row at the
binding watermark according to the writer shape that produced it without
weakening the pre-commit gate.

Authoritative source: DAEMON §4.2 (validate-before-commit) + IMPLEMENTATION-PLAN
§2.7 + the recovered ``validate()`` L618 discipline. This is the per-node
GENERALIZE of that recovered function: we keep ONLY its discipline — the flat
``(errors, warnings)`` contract, errors-block / warnings-allow, candidate +
wal-tail inputs — and DROP the 400-line reviewer-loop / workboard schema (no
cross-file workboard checks, no reviewer vocabulary).

Contract (frozen, IMPLEMENTATION-PLAN §2.7)::

    validate(candidate_binding: dict, wal_tail: list[dict]) -> (errors, warnings)

  * PURE. It WRITES NOTHING and MUTATES NOTHING (neither the candidate nor the
    wal_tail). It only inspects and returns the two flat lists of strings.
  * ``errors`` BLOCK commit (DAEMON §4.2 line 635: "if errors: abort —
    validate-before-commit: NOTHING written"). ``warnings`` ALLOW commit.

Call site (executor.transition, §2.6) hands ``wal_tail + [entry]``: the LAST WAL
record is the about-to-commit entry, carrying ``from_state`` / ``to_state`` /
``expected_generation`` / ``generation`` for THIS transition. The candidate is
the post-mutation node record (``state == entry.to_state``,
``generation == expected_generation + 1``).

The check-set (grounded in §4.2 + the recovered discipline):

  ERROR — an ILLEGAL lifecycle transition: ``states.is_legal(from_state,
          to_state)`` is False. The headline "illegal transition rejected before
          any write". EXCEPTION: a ``from_state == to_state`` no-op is NOT an
          illegal forward edge — it is the §3.6 ESCALATED slot-hold (the agent
          stamps a terminal_signal but legitimately keeps its slot; ``running``
          stays ``running``). A no-op is admitted (no error) but, when it carries
          a signal stamp, SURFACED as a WARNING rather than silently passed.

  ERROR — a MALFORMED candidate: a required §3.2 binding field is missing.

  ERROR — the per-node CAS post-condition (§4.2): ``candidate.generation`` MUST
          equal ``entry.expected_generation + 1``. A mismatch is a malformed
          candidate (a stale / wrong generation step) and blocks commit.

  WARNING — lesser issues that still ALLOW commit (the §3.6 ESCALATED no-op
          signal-stamp is the canonical per-node warning trigger).

FORK (disclosed, not silently chosen): §4.2 / §3.6 fix the legality-error and the
generation CAS post-condition as the load-bearing ERRORS, and route "lesser
issues" to warnings WITHOUT enumerating an exact lesser-issue set for per-node
v1. This module therefore pins exactly those two normative ERROR classes (plus
the missing-required-field structural error the tests mandate), admits a valid
candidate with empty errors, and routes the ESCALATED no-op signal-stamp to the
warnings channel. A benign unrecognized field is treated as fully benign (no
error, no warning) — it is not a transition violation. Other anomalies a future
builder wants to surface can be added to ``warnings`` without breaking this
contract.
"""

from __future__ import annotations

import harnessd.states as states
import harnessd.wal_policy as wal_policy

# The §3.2 binding fields that are LOAD-BEARING for the per-node CAS / lifecycle.
# A candidate missing any of these is structurally malformed for admission: the
# executor cannot CAS-fence (owner_token / generation), cannot address the node
# (node_address), and cannot resolve legality (state). This is intentionally the
# minimal CAS-bearing set, NOT the full §3.2 schema — per-node admission ports
# the DISCIPLINE, not the 400-line schema (IMPLEMENTATION-PLAN §2.7).
_REQUIRED_BINDING_FIELDS: tuple[str, ...] = (
    "node_address",
    "state",
    "generation",
    "owner_token",
)

GATE_STATES: tuple[str, ...] = (
    "candidate_submitted",
    "gate_bounced",
    "gate_escalated",
    "gate_failed",
    "gate_passed",
)

_GATE_STATE_SET = set(GATE_STATES)
_GATE_EVENT_TARGETS: dict[str, str] = {
    "gate_candidate_submitted": "candidate_submitted",
    "gate_bounced": "gate_bounced",
    "gate_escalated": "gate_escalated",
    "gate_failed": "gate_failed",
    "gate_passed": "gate_passed",
}
_GATE_ALLOWED_PREVIOUS: dict[str, set[str | None]] = {
    "candidate_submitted": {None, "gate_bounced", "gate_failed"},
    "gate_bounced": {"candidate_submitted", "gate_bounced", "gate_escalated"},
    "gate_escalated": {"candidate_submitted", "gate_bounced", "gate_escalated"},
    "gate_failed": {None, "candidate_submitted", "gate_bounced", "gate_escalated", "gate_failed"},
    "gate_passed": {"candidate_submitted", "gate_bounced", "gate_escalated"},
}

# The single writer never lets a generation-preserving own-slice accounting
# delta rewrite binding identity, lifecycle, CAS, or replay-watermark fields.
# Those fields are authoritative outside ``binding_delta``.  Keeping this
# structural boundary here prevents a forged row from masquerading as the
# accounting shape merely because its state and generation happen to match.
_OWN_SLICE_RESERVED_FIELDS: frozenset[str] = frozenset(
    {
        "node_address",
        "state",
        "generation",
        "owner_token",
        "lease_epoch",
        "last_applied_seq",
    }
)


def validate(
    candidate_binding: dict,
    wal_tail: list[dict],
) -> tuple[list[str], list[str]]:
    """Per-node admission check. PURE: returns ``(errors, warnings)``; writes nothing.

    ``errors`` block commit; ``warnings`` allow it (DAEMON §4.2). The last record
    of ``wal_tail`` is the about-to-commit entry for THIS transition.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # 1. Structural: required §3.2 binding fields must be present.
    #    (A missing field is a malformed candidate -> admission deny.)
    # ------------------------------------------------------------------
    for field in _REQUIRED_BINDING_FIELDS:
        if field not in candidate_binding:
            errors.append(
                f"malformed candidate: missing required binding field {field!r} (§3.2)"
            )

    # The about-to-commit entry is the LAST wal_tail record (executor hands
    # ``wal_tail + [entry]``). Without it there is no transition to validate.
    if not wal_tail:
        errors.append(
            "malformed wal_tail: no about-to-commit entry (executor passes wal_tail + [entry])"
        )
        return errors, warnings

    entry = wal_tail[-1]
    from_state = entry.get("from_state")
    to_state = entry.get("to_state")
    expected_generation = entry.get("expected_generation")

    # ------------------------------------------------------------------
    # 2. Legality gate (DAEMON §4.2 legality gate; the headline error).
    #    A from==to no-op is NOT an illegal forward edge: it is the §3.6
    #    ESCALATED slot-hold (signal stamped, lifecycle state unchanged).
    #    We admit the no-op but flag a signal-bearing no-op as a WARNING.
    # ------------------------------------------------------------------
    if from_state == to_state:
        # Legitimate self-loops are explicit slot-holds:
        #   * ESCALATED: the agent keeps context while waiting for an answer.
        #   * gate candidate submission: the producer keeps context while #review owns PASS/BOUNCE.
        #   * gate bounce: the producer keeps context while fixing typed gate defects.
        #   * gate escalation: the producer keeps context while the parent reviews non-convergence.
        if from_state == "running" and candidate_binding.get("terminal_signal") == "ESCALATED":
            warnings.append(
                f"ESCALATED slot-hold: no-op {from_state!r} -> {to_state!r} with "
                "terminal_signal=ESCALATED — admitted (§3.6: running stays running, lifecycle "
                "state unchanged), flagged as a benign signal-stamp no-op"
            )
        elif (
            from_state == "running"
            and candidate_binding.get("gate_state") == "candidate_submitted"
            and entry.get("event") == "gate_candidate_submitted"
        ):
            warnings.append(
                f"gate candidate submission: no-op {from_state!r} -> {to_state!r} with "
                "gate_state=candidate_submitted — admitted (GATE-LIFECYCLE: producer stays "
                "running while the review gate owns PASS/BOUNCE), flagged as a benign gate no-op"
            )
        elif (
            from_state == "running"
            and candidate_binding.get("gate_state") == "gate_bounced"
            and entry.get("event") == "gate_bounced"
        ):
            warnings.append(
                f"gate bounce: no-op {from_state!r} -> {to_state!r} with "
                "gate_state=gate_bounced — admitted (GATE-LIFECYCLE: producer keeps context while "
                "fixing typed review defects), flagged as a benign gate no-op"
            )
        elif (
            from_state == "running"
            and candidate_binding.get("gate_state") == "gate_escalated"
            and entry.get("event") == "gate_escalated"
        ):
            warnings.append(
                f"gate escalation: no-op {from_state!r} -> {to_state!r} with "
                "gate_state=gate_escalated — admitted (GATE-LIFECYCLE: bounce-cap exhaustion "
                "asks the parent while preserving producer context), flagged as a benign gate no-op"
            )
        elif (
            from_state == "running"
            and candidate_binding.get("gate_state") == "gate_failed"
            and entry.get("event") == "gate_failed"
        ):
            warnings.append(
                f"gate failure: no-op {from_state!r} -> {to_state!r} with "
                "gate_state=gate_failed — admitted (GATE-LIFECYCLE: review substrate failure "
                "is parent-visible while preserving the producer context), flagged as a benign gate no-op"
            )
        else:
            # Any OTHER self-loop is NOT a legal forward edge: a DONE/FAILED no-op (which must
            # change state to done/failed or enter the gate candidate path, not silently stay put),
            # or an unmotivated self-loop, is a malformed transition — is_legal returns False for
            # self-loops, so this blocks commit.
            errors.append(
                f"illegal no-op transition {from_state!r} -> {to_state!r}: only explicit slot-holds "
                "(ESCALATED, gate_state=candidate_submitted, gate_state=gate_bounced, or "
                "gate_state=gate_escalated, or gate_state=gate_failed) admit a self-loop; got "
                f"terminal_signal={candidate_binding.get('terminal_signal')!r}, "
                f"gate_state={candidate_binding.get('gate_state')!r}"
            )
    elif not states.is_legal(from_state, to_state):
        errors.append(
            f"illegal lifecycle transition {from_state!r} -> {to_state!r} "
            "(not in ALLOWED_TRANSITIONS; rejected before any write — DAEMON §4.2)"
        )

    # ------------------------------------------------------------------
    # 3. Per-node CAS post-condition (§4.2): candidate.generation MUST equal
    #    entry.expected_generation + 1. A mismatch is a malformed candidate.
    #    (Skipped if `generation` is absent — already reported above as missing.)
    # ------------------------------------------------------------------
    if "generation" in candidate_binding and expected_generation is not None:
        candidate_generation = candidate_binding["generation"]
        if candidate_generation != expected_generation + 1:
            errors.append(
                "malformed candidate: generation must equal expected_generation + 1 "
                f"(expected {expected_generation + 1}, got {candidate_generation!r}) "
                "— per-node CAS post-condition (§4.2)"
            )

    _validate_gate_state(candidate_binding, entry, from_state, to_state, errors)

    return errors, warnings


def validate_committed_snapshot(
    candidate_binding: dict,
    node_wal: list[dict],
) -> tuple[list[str], list[str]]:
    """Validate one durable binding against its own committed WAL rows.

    This is a READ contract, not an alternate write gate.  The binding's
    ``last_applied_seq`` selects the row whose delta reached the checkpoint:

    * a real lifecycle state change and every gate event retain the strict
      :func:`validate` semantics;
    * every other binding-applying effect is admitted only when its state,
      generation relation, identity, delta, and watermark exactly match the
      checkpoint;
    * journal-only rows cannot be the anchor because they never advance the
      watermark and carry null generations.

    A fresh generation-zero ``planned`` registration may have no node-owned
    row: re-registration seeds its watermark from the global WAL solely to
    fence prior incarnations.  That one structural registration shape is
    validated without inventing or borrowing a foreign transition — including
    when a retired incarnation's committed row sits at the seeded watermark,
    which is admissible history rather than this incarnation's effect.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for field in _REQUIRED_BINDING_FIELDS:
        if field not in candidate_binding:
            errors.append(
                f"malformed candidate: missing required binding field {field!r} (§3.2)"
            )
    if errors:
        return errors, warnings

    watermark = candidate_binding.get("last_applied_seq")
    if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < 0:
        errors.append(
            "malformed committed binding: last_applied_seq must be a non-negative integer, "
            f"got {watermark!r}"
        )
        return errors, warnings

    address = candidate_binding["node_address"]
    owner_token = candidate_binding.get("owner_token")
    lease_epoch = candidate_binding.get("lease_epoch")
    # An epoch-fenced row is a prior incarnation's committed history, so it can never be
    # THIS incarnation's applied effect — even when the re-registration watermark lands on
    # its seq (``chokepoint.reregister_identity_seed`` seeds the global max).  Dropping it
    # from the anchor set routes that shape into the rowless-registration branch below
    # rather than validating a fresh registration against the incarnation it replaced.
    address_rows = [
        row
        for row in node_wal
        if row.get("node_address") == address
        and isinstance(row.get("seq"), int)
        and not isinstance(row.get("seq"), bool)
        and row["seq"] <= watermark
        and not wal_policy.epoch_fenced(candidate_binding, row)
    ]
    anchors = [row for row in address_rows if row["seq"] == watermark]

    if not anchors:
        if (
            candidate_binding.get("state") == "planned"
            and candidate_binding.get("generation") == 0
        ):
            return errors, warnings
        errors.append(
            f"malformed committed binding: no committed WAL row for current incarnation "
            f"at last_applied_seq={watermark!r} for {address!r}"
        )
        return errors, warnings
    if len(anchors) != 1:
        errors.append(
            f"malformed committed binding: expected exactly one WAL row at "
            f"last_applied_seq={watermark!r} for {address!r}, got {len(anchors)}"
        )
        return errors, warnings

    entry = anchors[0]
    expected_generation = entry.get("expected_generation")
    generation = entry.get("generation")

    # A journal-only row never updates the binding watermark.  Seeing one at
    # the checkpoint therefore means the committed snapshot is malformed.
    if expected_generation is None or generation is None:
        errors.append(
            f"malformed committed binding: journal-only event {entry.get('event')!r} "
            f"cannot own last_applied_seq={watermark!r}"
        )
        return errors, warnings

    state = candidate_binding.get("state")
    from_state = entry.get("from_state")
    to_state = entry.get("to_state")
    candidate_generation = candidate_binding.get("generation")

    if entry.get("actor") != "harnessd":
        errors.append(
            "malformed committed binding effect: actor must be the single writer "
            f"'harnessd', got {entry.get('actor')!r}"
        )
    if entry.get("owner_token") != owner_token:
        errors.append(
            "malformed committed binding effect: owner_token does not match checkpoint "
            f"(row={entry.get('owner_token')!r}, binding={owner_token!r})"
        )
    if entry.get("lease_epoch") != lease_epoch:
        errors.append(
            "malformed committed binding effect: lease_epoch does not match checkpoint "
            f"(row={entry.get('lease_epoch')!r}, binding={lease_epoch!r})"
        )
    if to_state != state:
        errors.append(
            "malformed committed binding effect: to_state must equal binding state "
            f"{state!r}, got {to_state!r}"
        )
    if generation != candidate_generation:
        errors.append(
            "malformed committed binding effect: WAL generation must equal binding "
            f"generation {candidate_generation!r}, got {generation!r}"
        )
    if (
        not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation not in {expected_generation, expected_generation + 1}
    ):
        errors.append(
            "malformed committed binding effect: generation must stay unchanged or "
            "advance by exactly one "
            f"(expected_generation={expected_generation!r}, generation={generation!r})"
        )

    delta = entry.get("binding_delta")
    if not isinstance(delta, dict):
        errors.append("malformed committed binding effect: binding_delta must be an object")
        delta = {}
    for field, value in delta.items():
        if field not in candidate_binding or candidate_binding[field] != value:
            errors.append(
                "malformed committed binding effect: binding_delta does not match "
                f"checkpoint field {field!r} (row={value!r}, "
                f"binding={candidate_binding.get(field)!r})"
            )

    # Real state changes and every gate event keep the strict lifecycle/gate
    # semantics.  Same-state message, receipt, and accounting effects are
    # committed binding writes rather than lifecycle transitions.
    strict_effect = from_state != to_state or entry.get("event") in _GATE_EVENT_TARGETS
    if strict_effect:
        strict_errors, strict_warnings = validate(candidate_binding, [entry])
        errors.extend(strict_errors)
        warnings.extend(strict_warnings)
        return errors, warnings

    if from_state != state:
        errors.append(
            "malformed committed binding effect: same-state effect must start from "
            f"binding state {state!r}, got {from_state!r}"
        )
    if not delta:
        errors.append(
            f"illegal empty no-op transition {from_state!r} -> {to_state!r}: "
            "a non-gate same-state commit must carry a binding effect"
        )
    if expected_generation == generation:
        reserved = sorted(_OWN_SLICE_RESERVED_FIELDS.intersection(delta))
        if reserved:
            errors.append(
                "malformed committed own-slice row: binding_delta writes reserved "
                f"binding field(s) {reserved!r}"
            )
    _validate_gate_state(candidate_binding, entry, from_state, to_state, errors)
    return errors, warnings


def _validate_gate_state(
    candidate_binding: dict,
    entry: dict,
    from_state: str,
    to_state: str,
    errors: list[str],
) -> None:
    gate_state = candidate_binding.get("gate_state")
    event = entry.get("event")
    delta = entry.get("binding_delta") or {}
    if gate_state is not None and gate_state not in _GATE_STATE_SET:
        errors.append(
            f"illegal gate_state {gate_state!r}: expected one of {sorted(_GATE_STATE_SET)} "
            "(GATE-LIFECYCLE state model)"
        )
    if event not in _GATE_EVENT_TARGETS:
        return

    target_gate_state = _GATE_EVENT_TARGETS[event]
    delta_gate_state = delta.get("gate_state")
    if gate_state != target_gate_state or delta_gate_state != target_gate_state:
        errors.append(
            f"illegal gate transition for event {event!r}: expected gate_state "
            f"{target_gate_state!r} in candidate and binding_delta, got "
            f"candidate={gate_state!r}, delta={delta_gate_state!r}"
        )
        return

    previous = delta.get("gate_state_before")
    if previous not in _GATE_ALLOWED_PREVIOUS[target_gate_state]:
        errors.append(
            f"illegal gate transition {previous!r} -> {target_gate_state!r} for event {event!r} "
            f"(allowed previous states: {sorted(str(s) for s in _GATE_ALLOWED_PREVIOUS[target_gate_state])})"
        )
    if target_gate_state == "gate_passed" and to_state != "done":
        errors.append(
            "illegal gate transition to 'gate_passed': producer lifecycle state must move to 'done' "
            f"on gate_passed, got {from_state!r} -> {to_state!r}"
        )
