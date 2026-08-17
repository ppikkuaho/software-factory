"""The single-writer executor — the KEYSTONE transition primitive (CAS + intent-first commit).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.6 (the frozen executor.py interface — exact signatures transcribed
    into the functions below) + §3 module table (executor.py row).
  - DAEMON §4.2 (the 3-precondition CAS, ALL checked BEFORE any mutation; legality gate;
    validate-before-commit), §4.3 (ONE EX serialization domain), §4.4 (intent-first commit
    ordering: append_wal — the INTENT — FIRST, then write_binding — the CHECKPOINT — SECOND).
  - DAEMON §6.1 (the claim-before-spawn slot-claim: a transition variant into ``claimed`` that
    mints a NEW owner_token + bumped lease_epoch, fencing the prior incarnation), §8 (the
    self-fencing owner_token).
  - DAEMON §3.5 (the run-ledger WAL record field set) + §3.2 (binding record).

THE single code path that mutates binding state. Everything funnels here; nothing else writes the
ledger except ``commit`` (which is private, called only from ``transition`` inside the held lock).

The whole transition body runs inside ONE ``store.file_lock(EX)`` — the one serialization domain
(§4.3). The lock file is ``.harnessd.lock`` under ``ledger.RUNTIME_ROOT`` (DAEMON §2.3 / §4.3 names
the single exclusive ``fcntl.flock`` on ``.harnessd.lock``; the same root the ledger's WAL/binding
files land under, so the lock co-locates with the state it serializes).

CAS DISCIPLINE (§4.2 — load-bearing, each mutant-killing):
  The THREE CAS preconditions (expected_state / per-node expected_generation / expected_owner_token)
  are checked BEFORE any candidate is built and BEFORE any write — accumulate-then-abort-before-write.
  Each aborts INDEPENDENTLY. The fencing (owner_token) abort is NON-DESTRUCTIVE: it journals a
  ``stale_return_ignored`` WAL row (§3.6 FENCED) and leaves the live binding byte-for-byte UNCHANGED.

INTENT-FIRST COMMIT (§4.4 — the durability keystone):
  ``commit`` stamps ``last_applied_seq = entry.seq`` into the candidate, then appends the WAL entry
  (the INTENT) FIRST and writes the binding (the CHECKPOINT) SECOND. A crash BETWEEN the two leaves
  WAL-ahead-of-binding => the event is REPLAYABLE (its seq exceeds the binding's watermark). A crash
  BEFORE the append leaves nothing — the actor's CAS simply retries.

RESOLVED DETAILS (unspecified by the frozen tests; decided spec-faithfully, surfaced to the
orchestrator):
  * LOCK PATH — ``ledger.RUNTIME_ROOT / ".harnessd.lock"`` (the §4.3 single serialization domain).
    A pathless executor (the tests bind only ``ledger.RUNTIME_ROOT``) needs the lock co-located with
    the WAL/binding it serializes; ``.harnessd.lock`` is the name the plan/DAEMON pin (§2.3 tree).
  * MERGE-NODE-INTO-MAP — ``commit`` reads the WHOLE keyed binding map fresh via
    ``ledger.all_nodes()`` (inside the held EX lock, so no concurrent writer), splices the candidate
    in at ``node_address``, and hands the whole map to ``ledger.write_binding(..., _lock_held=True)``
    (the Option-A whole-map atomic-replace). Reading fresh under the lock preserves every OTHER node's
    slice (no cross-node clobber).
  * HANDOFF PACKET (§4.4 step 3 "regenerate derived handoff packet") — a THIN STUB in v1: the
    derived continuation/next-action packet is a later-increment artifact (chokepoint / reconcile
    territory). ``commit`` calls ``_regenerate_handoff_packet`` which is a documented no-op here; the
    durable journal (WAL + binding) is complete without it, and it is purely DERIVED (regenerable
    from committed state), so deferring it does not affect crash-replayability. NOTED.
"""

from __future__ import annotations

import copy
from typing import NamedTuple, Optional

from . import clock, fencing, ledger, states, store, validate

# ---------------------------------------------------------------------------
# The single serialization-domain lock file name (DAEMON §2.3 / §4.3). The lock
# co-locates with the WAL + binding under RUNTIME_ROOT so a pathless executor
# (tests bind only ledger.RUNTIME_ROOT) serializes against the state it guards.
# ---------------------------------------------------------------------------

LOCK_FILENAME: str = ".harnessd.lock"


def _resolve_lock_path():
    """Resolve the EX serialization-domain lock path: ``RUNTIME_ROOT / .harnessd.lock``.

    Mirrors the ledger's path-injection contract (§2.4): the daemon binds ``ledger.RUNTIME_ROOT``
    once at startup; tests bind it to ``tmp_path``. A missing root is a misconfiguration, surfaced
    loud (the executor cannot serialize without a home for its lock).
    """
    if ledger.RUNTIME_ROOT is None:
        raise RuntimeError(
            "executor lock path is not configured: bind ledger.RUNTIME_ROOT "
            "(the executor co-locates .harnessd.lock with the WAL/binding it serializes)"
        )
    from pathlib import Path

    return Path(ledger.RUNTIME_ROOT) / LOCK_FILENAME


def lock_path():
    """The canonical §4.3 per-mutation EX lock path — exposed for reconcile's replay critical
    section (the boot-replay checkpoint takes this SAME lock so its ``_lock_held=True`` is
    true-by-fact, F6/reconcile-2). Internal call sites keep using ``_resolve_lock_path``."""
    return _resolve_lock_path()


# ---------------------------------------------------------------------------
# Result types (§2.6 frozen).
# ---------------------------------------------------------------------------

class TransitionResult(NamedTuple):
    """The outcome of a transition()/claim() call (§2.6).

    ``ok``       — True iff the transition committed; False on ANY abort (a CAS miss, an illegal
                   edge, a validate error, a fencing rejection).
    ``errors``   — the abort reasons (a CAS/legality/fencing abort carries one structured reason;
                   a validate abort carries validate's error list). Empty on success.
    ``warnings`` — validate's warnings (allowed-but-flagged); empty unless validate ran and warned.
    ``binding``  — the resulting binding. On commit, the post-commit candidate (generation+1, delta
                   applied, watermark stamped). On abort, the LIVE binding UNCHANGED (or None if the
                   node was absent).
    """

    ok: bool
    errors: list
    warnings: list
    binding: Optional[dict]


class CheckpointResult(NamedTuple):
    """The outcome of a watchdog_checkpoint() call (§2.6).

    ``ok``       — True iff the own-slice liveness write committed (or was a no-op steady-healthy poll).
    ``appended`` — True iff a run-ledger row was appended (edge-triggered: False on a steady-healthy
                   poll that wrote no WAL row).
    ``binding``  — the resulting binding (post-write own-slice), or the live binding on a no-op/abort.
    ``errors``   — abort reasons (e.g. a stale owner_token fencing rejection); empty on success.
    """

    ok: bool
    appended: bool
    binding: Optional[dict]
    errors: list


# ---------------------------------------------------------------------------
# transition() — the CAS-guarded, lock-serialized, validate-before-commit primitive (§2.6 / §4.2).
# ---------------------------------------------------------------------------

def transition(
    node_address: str,
    *,
    expected_state: str,
    expected_generation: int,
    expected_owner_token: Optional[str],
    target_state: str,
    binding_delta: dict,
    new_lease_epoch: Optional[int] = None,
    new_owner_token: Optional[str] = None,
    event: str,
    actor: str = "harnessd",
    summary: str = "",
    artifacts: Optional[list] = None,
    related_binding_deltas: Optional[dict[str, dict]] = None,
    related_expected_owner_tokens: Optional[dict[str, Optional[str]]] = None,
) -> TransitionResult:
    """The single state-changing primitive (§2.6 / §4.2). Whole body inside ONE EX lock (§4.3).

    Order (every gate BEFORE any write — accumulate-then-abort-before-write, §4.2):
      1. read the live binding;
      2. THREE CAS preconditions (expected_state, per-node expected_generation, expected_owner_token)
         — each aborts INDEPENDENTLY; the owner_token (fencing) miss journals ``stale_return_ignored``
         and leaves the live binding UNCHANGED;
      3. legality gate (``states.is_legal(binding.state, target_state)``);
      4. build the candidate (deepcopy + apply binding_delta + generation += 1; rotate
         lease_epoch/owner_token IN THE SAME candidate when given — F-012, no split window);
      5. build the WAL entry and run ``validate(candidate, load_wal() + [entry])`` — an ERROR aborts
         with NOTHING written (validate-before-commit);
      6. ``commit(candidate, entry)`` — intent-first (§4.4).
    """
    artifacts = artifacts if artifacts is not None else []
    related_binding_deltas = related_binding_deltas or {}
    related_expected_owner_tokens = related_expected_owner_tokens or {}

    with store.file_lock(_resolve_lock_path(), shared=False):
        binding = ledger.read_binding(node_address)

        # ------------------------------------------------------------------
        # Precondition 0 (structural): the node must exist to transition it.
        # ------------------------------------------------------------------
        if binding is None:
            return TransitionResult(
                ok=False,
                errors=[f"no binding for node {node_address!r}: cannot transition an absent node"],
                warnings=[],
                binding=None,
            )

        # ------------------------------------------------------------------
        # The THREE CAS preconditions (§4.2) — ALL checked BEFORE any mutation.
        # Accumulate-then-abort-before-write: no candidate is built, NOTHING is
        # written, until every precondition has passed. Each is INDEPENDENTLY
        # load-bearing (the load-bearing tests drop one each and prove a stale
        # call commits if the check is missing).
        # ------------------------------------------------------------------

        # (1) expected_state — the recovered expected-state guard (L1511).
        if binding["state"] != expected_state:
            return TransitionResult(
                ok=False,
                errors=[
                    f"CAS abort (state): live state {binding['state']!r} != "
                    f"expected_state {expected_state!r}"
                ],
                warnings=[],
                binding=binding,
            )

        # (2) expected_generation — PER-NODE generation (NOT global len(ledger)).
        if binding["generation"] != expected_generation:
            return TransitionResult(
                ok=False,
                errors=[
                    f"CAS abort (generation): live per-node generation "
                    f"{binding['generation']!r} != expected_generation {expected_generation!r}"
                ],
                warnings=[],
                binding=binding,
            )

        # (3) expected_owner_token — the FENCING precondition. A stale token (lower-epoch /
        # different-incarnation) loses to the live one. The abort is NON-DESTRUCTIVE: it
        # JOURNALS a stale_return_ignored WAL row and leaves the live binding UNCHANGED.
        if expected_owner_token is not None and binding["owner_token"] != expected_owner_token:
            fenced_entry = ledger.build_wal_record(
                node_address=node_address,
                event="stale_return_ignored",
                from_state=binding["state"],
                to_state=binding["state"],  # state UNCHANGED — the live owner keeps it
                expected_generation=expected_generation,
                generation=binding["generation"],  # NO generation bump — nothing committed
                lease_epoch=binding.get("lease_epoch"),
                owner_token=binding["owner_token"],  # the LIVE token (the one that holds the slot)
                binding_delta={"presented_owner_token": expected_owner_token},
                summary="stale owner_token presented; de-authorized (non-destructive, §3.6 FENCED)",
                artifacts=[],
                seq=ledger.next_seq(),
            )
            # Journal ONLY the WAL row — DO NOT write_binding (the live binding is left UNCHANGED).
            ledger.append_wal(fenced_entry)
            return TransitionResult(
                ok=False,
                errors=[
                    f"fencing abort (owner_token): presented {expected_owner_token!r} != "
                    f"live {binding['owner_token']!r} — journaled stale_return_ignored, "
                    "binding UNCHANGED"
                ],
                warnings=[],
                binding=binding,
            )

        # ------------------------------------------------------------------
        # Legality gate (§4.2) — an illegal target aborts BEFORE the candidate is built.
        # CARVE-OUT (SML-02): a from==to SELF-LOOP is NOT a forward edge — the table
        # deliberately contains no self-loops — so it falls through to validate-before-
        # commit, which admits ONLY the §3.6 ESCALATED slot-hold (running→running with
        # terminal_signal=ESCALATED, validate.py) and ERRORS every other no-op. Nothing
        # is ever written for a non-ESCALATED self-loop (validate aborts pre-commit).
        # ------------------------------------------------------------------
        if target_state != binding["state"] and not states.is_legal(binding["state"], target_state):
            return TransitionResult(
                ok=False,
                errors=[
                    f"illegal transition {binding['state']!r} -> {target_state!r} "
                    "(not in ALLOWED_TRANSITIONS; rejected before any write — §4.2)"
                ],
                warnings=[],
                binding=binding,
            )

        # ------------------------------------------------------------------
        # Build the candidate: deepcopy the live binding, apply the delta, bump
        # the per-node generation. Rotate lease_epoch/owner_token IN THE SAME
        # candidate (F-012 — no window where state advanced but ownership did not).
        # ------------------------------------------------------------------
        candidate = copy.deepcopy(binding)
        candidate.update(binding_delta)
        # Identity + lifecycle state are authoritative from the VALIDATED arguments, never the delta:
        #   - node_address is the CAS-validated key commit() merges the candidate back on (a delta that
        #     carried a different node_address would re-key the whole-map merge onto the WRONG node).
        #   - state IS target_state (the edge the legality gate just approved); sourcing it from the delta
        #     instead would let candidate.state silently diverge from the legality-checked target.
        candidate["node_address"] = node_address
        candidate["state"] = target_state
        candidate["generation"] = binding["generation"] + 1
        if new_lease_epoch is not None:
            candidate["lease_epoch"] = new_lease_epoch
        if new_owner_token is not None:
            candidate["owner_token"] = new_owner_token

        related_candidates: dict[str, dict] = {}
        related_wal: dict[str, dict] = {}
        for related_address, related_delta in related_binding_deltas.items():
            related_binding = ledger.read_binding(related_address)
            if related_binding is None:
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"related transition target {related_address!r} is absent"
                    ],
                    warnings=[],
                    binding=binding,
                )
            expected_related_token = related_expected_owner_tokens.get(related_address)
            if (
                expected_related_token is not None
                and related_binding.get("owner_token") != expected_related_token
            ):
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"related transition fencing abort for {related_address!r}: "
                        f"presented {expected_related_token!r} != "
                        f"live {related_binding.get('owner_token')!r}"
                    ],
                    warnings=[],
                    binding=binding,
                )
            related_candidate = copy.deepcopy(related_binding)
            related_candidate.update(copy.deepcopy(related_delta))
            related_candidate["node_address"] = related_address
            related_candidate["generation"] = int(related_binding.get("generation") or 0) + 1
            related_candidates[related_address] = related_candidate
            related_wal[related_address] = {
                "from_state": related_binding.get("state"),
                "to_state": related_binding.get("state"),
                "expected_generation": related_binding.get("generation"),
                "generation": related_candidate["generation"],
                "lease_epoch": related_binding.get("lease_epoch"),
                "owner_token": related_binding.get("owner_token"),
                "binding_delta": copy.deepcopy(related_delta),
            }

        # ------------------------------------------------------------------
        # Build the about-to-commit WAL entry, then validate the candidate against
        # the WAL tail + this entry (the entry is the LAST record — validate reads
        # its from/to_state + expected_generation). An ERROR aborts with NOTHING
        # written (validate-before-commit, §4.2).
        # ------------------------------------------------------------------
        entry = ledger.build_wal_record(
            node_address=node_address,
            event=event,
            from_state=binding["state"],
            to_state=target_state,
            expected_generation=expected_generation,
            generation=candidate["generation"],  # post-commit generation (= expected + 1)
            lease_epoch=candidate.get("lease_epoch"),
            owner_token=candidate.get("owner_token"),
            binding_delta=binding_delta,
            summary=summary,
            artifacts=artifacts,
            seq=ledger.next_seq(),
            related_binding_deltas=related_wal,
        )

        errors, warnings = validate.validate(candidate, ledger.load_wal() + [entry])
        if errors:
            return TransitionResult(ok=False, errors=errors, warnings=warnings, binding=binding)

        # ------------------------------------------------------------------
        # Commit — intent-first (§4.4). Inside the held EX lock.
        # ------------------------------------------------------------------
        if related_candidates:
            _commit_binding_candidates(
                {node_address: candidate, **related_candidates},
                entry,
            )
        else:
            commit(candidate, entry)

        return TransitionResult(ok=True, errors=[], warnings=warnings, binding=candidate)


# ---------------------------------------------------------------------------
# claim() — §6.1 STEP-1 slot-claim: a transition variant into 'claimed' that mints a NEW
# owner_token and a bumped lease_epoch (fencing the prior incarnation). §6.4 re-adopt edges.
# ---------------------------------------------------------------------------

def claim(
    node_address: str,
    *,
    expected_state: str,
    expected_generation: int,
    expected_owner_token: Optional[str],
    level_config,
) -> TransitionResult:
    """The spawn-chokepoint slot-claim (§6.1 STEP-1 / §6.4 re-adopt).

    A ``transition`` variant: ``target_state='claimed'``; ``expected_state ∈ {planned (fresh),
    running (resume-live §6.4), dead (necro §5)}``. Mints a NEW ``owner_token`` at a BUMPED
    ``lease_epoch`` (``fencing.advance_epoch`` + ``fencing.mint_owner_token``), rotated in the SAME
    candidate as the state change (F-012). The bumped epoch is embedded in the new token, so the
    prior incarnation's token (lower epoch) is fenced out the moment this claim commits.

    The subagent_id / session_uuid carried into the minted token are the binding's CURRENT identity
    (the re-adopt re-takes the SAME seat under a higher epoch; a fresh spawn from ``planned`` simply
    re-mints the placeholder identity at epoch+1 — the chokepoint records the real session_uuid in a
    later STEP-4 transition once the actor opens, §6.1). ``level_config`` is threaded for the seat's
    config-time identity and reserved for the §6.1 admission seat; the token identity is read off the
    live binding so the mint is a pure function of committed state.
    """
    # Read the live binding to source the seat identity (subagent_id / session_uuid / current epoch)
    # for the minted token. The authoritative CAS re-reads under the lock inside transition(); this
    # pre-read only assembles the rotation inputs (a stale read here cannot commit — transition()'s
    # CAS is the gate).
    live = ledger.read_binding(node_address)
    if live is None:
        return TransitionResult(
            ok=False,
            errors=[f"no binding for node {node_address!r}: cannot claim an absent slot"],
            warnings=[],
            binding=None,
        )

    new_lease_epoch = fencing.advance_epoch(live)  # old + 1 (DAEMON §8)
    new_owner_token = fencing.mint_owner_token(
        node_address,
        live.get("subagent_id"),
        live.get("session_uuid"),
        new_lease_epoch,
    )

    return transition(
        node_address,
        expected_state=expected_state,
        expected_generation=expected_generation,
        expected_owner_token=expected_owner_token,
        target_state="claimed",
        binding_delta={
            "state": "claimed",
            "lease_epoch": new_lease_epoch,
            "owner_token": new_owner_token,
            # A claim opens a fresh actor incarnation. Blocked-input cancellation budget and
            # active incident state are incarnation-local; durable WAL history remains intact.
            "seat_stall_active": False,
            "seat_stall_incident_id": None,
            "seat_stall_since": None,
            "seat_stall_classification": None,
            "seat_stall_silent_seconds": None,
            "seat_stall_pane_excerpt": None,
            "seat_stall_prompt_signature": None,
            "seat_stall_cancel_status": None,
            "seat_stall_positive_incident_count": 0,
            "seat_stall_retriggered": False,
            "seat_stall_escalated": False,
            "seat_stall_root_limit": False,
            "seat_stall_actioned_at": None,
            "seat_stall_recovered_at": None,
            "seat_stall_recovery_reason": None,
        },
        new_lease_epoch=new_lease_epoch,
        new_owner_token=new_owner_token,
        event="claim",
        summary="slot-claim: re-mint owner_token at bumped lease_epoch (fences prior incarnation, §6.1/§8)",
    )


# ---------------------------------------------------------------------------
# commit() — PRIVATE, intent-first crash-atomicity (§4.4). Called ONLY from transition()/the other
# mutators, INSIDE the held EX lock (write_binding asserts _lock_held=True).
# ---------------------------------------------------------------------------

def commit(candidate_binding: dict, entry: dict) -> None:
    """INTENT-FIRST commit (§4.4). PRIVATE — runs INSIDE the caller's held EX lock.

    Ordering (the load-bearing durability invariant):
      1. stamp ``candidate_binding['last_applied_seq'] = entry['seq']`` (the replay watermark, IN the
         checkpoint);
      2. ``ledger.append_wal(entry)`` — the INTENT, FIRST (ONE framed write + fsync);
      3. ``ledger.write_binding(<whole map with candidate merged in>, _lock_held=True)`` — the
         CHECKPOINT, SECOND (tmp + fsync + os.replace);
      4. regenerate the derived handoff packet (a thin stub in v1 — see ``_regenerate_handoff_packet``).

    A crash BETWEEN (2) and (3) leaves the WAL entry on disk with the binding UNCHANGED =>
    WAL-ahead-of-binding => REPLAYABLE (the event's seq exceeds the binding's last_applied_seq). A
    crash BEFORE (2) leaves nothing — the actor's CAS retries. Reversing (2) and (3) would yield
    binding-ahead-of-WAL, the un-replayable failure mode (the crash-replayability test catches it).
    """
    # (1) Stamp the watermark INTO the checkpoint candidate before either write.
    candidate_binding["last_applied_seq"] = entry["seq"]

    # (2) The INTENT — append the WAL entry FIRST (one framed write + fsync).
    ledger.append_wal(entry)

    # (3) The CHECKPOINT — merge the candidate into the WHOLE keyed map and atomic-replace it.
    # Read the whole map fresh under the held EX lock (no concurrent writer can interleave), so
    # every OTHER node's slice is preserved (no cross-node clobber, §4.3).
    whole_map = ledger.all_nodes()
    whole_map[candidate_binding["node_address"]] = candidate_binding
    ledger.write_binding(whole_map, _lock_held=True)

    # (4) Regenerate the derived handoff packet (thin stub in v1 — see module docstring).
    _regenerate_handoff_packet(candidate_binding, entry)


def _commit_binding_candidates(candidates: dict[str, dict], entry: dict) -> None:
    """Intent-first checkpoint of multiple binding slices under the caller-held EX lock.

    This is deliberately narrow: canonical message answers use it to create the answer record and
    close the referenced question as one ledger event. No independent secondary truth is written.
    """
    for candidate in candidates.values():
        candidate["last_applied_seq"] = entry["seq"]
    ledger.append_wal(entry)
    whole_map = ledger.all_nodes()
    whole_map.update(candidates)
    ledger.write_binding(whole_map, _lock_held=True)


def record_message(
    sender_address: str,
    *,
    message_id: str,
    record: dict,
    answers_question: Optional[dict] = None,
    extra_sender_delta: Optional[dict] = None,
    event: Optional[str] = None,
    summary: Optional[str] = None,
) -> TransitionResult:
    """Commit one immutable sender-owned message and, for an answer, close its question atomically."""
    with store.file_lock(_resolve_lock_path(), shared=False):
        sender = ledger.read_binding(sender_address)
        if sender is None:
            return TransitionResult(
                ok=False,
                errors=[f"no binding for message sender {sender_address!r}"],
                warnings=[],
                binding=None,
            )

        existing = (sender.get("messages") or {}).get(message_id)
        if existing is not None:
            if existing.get("content_sha256") != record.get("content_sha256"):
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"message id {message_id!r} already exists with different immutable content"
                    ],
                    warnings=[],
                    binding=sender,
                )
            return TransitionResult(ok=True, errors=[], warnings=[], binding=sender)

        sender_messages = copy.deepcopy(sender.get("messages") or {})
        sender_messages[message_id] = copy.deepcopy(record)
        sender_candidate = copy.deepcopy(sender)
        sender_candidate["messages"] = sender_messages
        sender_candidate["message_last_id"] = message_id
        sender_candidate.update(copy.deepcopy(extra_sender_delta or {}))
        sender_candidate["generation"] = int(sender.get("generation") or 0) + 1
        candidates = {sender_address: sender_candidate}
        related: dict = {}

        if answers_question:
            asker_address = str(answers_question.get("asker_address") or "")
            question_id = str(answers_question.get("message_id") or "")
            asker = ledger.read_binding(asker_address)
            if asker is None:
                return TransitionResult(
                    ok=False,
                    errors=[f"answer references absent asker binding {asker_address!r}"],
                    warnings=[],
                    binding=sender,
                )
            questions = copy.deepcopy(asker.get("messages") or {})
            question = questions.get(question_id)
            if not isinstance(question, dict) or not question.get("needs_answer"):
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"answer references unknown open-question message "
                        f"({asker_address!r}, {question_id!r})"
                    ],
                    warnings=[],
                    binding=sender,
                )
            if question.get("target") != sender_address:
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"only question target {question.get('target')!r} may answer "
                        f"({asker_address!r}, {question_id!r})"
                    ],
                    warnings=[],
                    binding=sender,
                )
            state = question.get("question_state")
            answer_ref = {"answerer_address": sender_address, "message_id": message_id}
            if state == "answered" and question.get("answered_by") == answer_ref:
                return TransitionResult(ok=True, errors=[], warnings=[], binding=sender)
            if state != "open":
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"question ({asker_address!r}, {question_id!r}) is {state!r}, not open"
                    ],
                    warnings=[],
                    binding=sender,
                )
            question["question_state"] = "answered"
            question["answered_by"] = answer_ref
            question["answered_at"] = clock.now_utc()
            questions[question_id] = question
            asker_candidate = copy.deepcopy(asker)
            asker_candidate["messages"] = questions
            asker_candidate["generation"] = int(asker.get("generation") or 0) + 1
            candidates[asker_address] = asker_candidate
            related[asker_address] = {
                "from_state": asker.get("state"),
                "to_state": asker.get("state"),
                "expected_generation": asker.get("generation"),
                "generation": asker_candidate["generation"],
                "lease_epoch": asker.get("lease_epoch"),
                "owner_token": asker.get("owner_token"),
                "binding_delta": {"messages": questions},
            }

        entry = ledger.build_wal_record(
            node_address=sender_address,
            event=event or ("message_answered" if answers_question else "message_recorded"),
            from_state=sender.get("state"),
            to_state=sender.get("state"),
            expected_generation=sender.get("generation"),
            generation=sender_candidate["generation"],
            lease_epoch=sender.get("lease_epoch"),
            owner_token=sender.get("owner_token"),
            binding_delta={
                "messages": sender_messages,
                "message_last_id": message_id,
                **copy.deepcopy(extra_sender_delta or {}),
            },
            summary=summary or (
                f"canonical answer message {message_id} recorded"
                if answers_question
                else f"canonical message {message_id} recorded"
            ),
            artifacts=[
                value
                for value in (record.get("marker"), record.get("artifact"))
                if value
            ],
            seq=ledger.next_seq(),
            related_binding_deltas=related,
        )
        _commit_binding_candidates(candidates, entry)
        return TransitionResult(
            ok=True,
            errors=[],
            warnings=[],
            binding=sender_candidate,
        )


def record_contract_rebind(
    holder_address: str,
    *,
    rebind_id: str,
    contract_path: str,
    receipt: dict,
    expected_owner_token: str,
    marker: str,
) -> TransitionResult:
    """Atomically replace one holder receipt after a verified node-local rebind marker.

    The caller has already checked revision lineage and current filesystem identity.  This
    executor operation owns the incarnation fence, immutable rebind-id idempotency, WAL intent,
    and receipt-map merge so concurrent control-plane writes cannot drop another contract row.
    """
    with store.file_lock(_resolve_lock_path(), shared=False):
        holder = ledger.read_binding(holder_address)
        if holder is None:
            return TransitionResult(
                ok=False,
                errors=[f"no binding for contract holder {holder_address!r}"],
                warnings=[],
                binding=None,
            )
        if holder.get("owner_token") != expected_owner_token:
            fenced_entry = ledger.build_wal_record(
                node_address=holder_address,
                event="stale_return_ignored",
                from_state=holder.get("state"),
                to_state=holder.get("state"),
                expected_generation=holder.get("generation"),
                generation=holder.get("generation"),
                lease_epoch=holder.get("lease_epoch"),
                owner_token=holder.get("owner_token"),
                binding_delta={
                    "presented_owner_token": expected_owner_token,
                    "attempted": "contract_rebind",
                },
                summary="stale owner_token on contract rebind; de-authorized",
                artifacts=[marker],
                seq=ledger.next_seq(),
            )
            ledger.append_wal(fenced_entry)
            return TransitionResult(
                ok=False,
                errors=[
                    f"fencing abort (owner_token) on contract_rebind: presented "
                    f"{expected_owner_token!r} != live {holder.get('owner_token')!r}"
                ],
                warnings=[],
                binding=holder,
            )

        rebinds = copy.deepcopy(holder.get("contract_rebinds") or {})
        immutable = {
            "contract_path": str(contract_path),
            "fingerprint": receipt.get("fingerprint"),
            "revision_record_ref": receipt.get("revision_record_ref"),
        }
        existing = rebinds.get(rebind_id)
        if existing is not None:
            if existing != immutable:
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"contract rebind id {rebind_id!r} already exists with different content"
                    ],
                    warnings=[],
                    binding=holder,
                )
            return TransitionResult(ok=True, errors=[], warnings=[], binding=holder)

        receipts = copy.deepcopy(holder.get("contract_receipts") or {})
        receipts[str(contract_path)] = copy.deepcopy(receipt)
        rebinds[rebind_id] = immutable
        candidate = copy.deepcopy(holder)
        candidate["contract_receipts"] = receipts
        candidate["contract_rebinds"] = rebinds
        candidate["contract_last_rebind_id"] = rebind_id
        compatibility_delta: dict = {}
        legacy_intent = holder.get("intent_spec_receipt")
        if (
            isinstance(legacy_intent, dict)
            and str(legacy_intent.get("artifact") or "") == str(contract_path)
        ):
            candidate["intent_spec_receipt"] = copy.deepcopy(receipt)
            compatibility_delta["intent_spec_receipt"] = copy.deepcopy(receipt)
        if str(holder.get("accepted_test_contract_home") or "") == str(contract_path):
            candidate["accepted_test_package_stamp"] = copy.deepcopy(receipt.get("stamp"))
            compatibility_delta["accepted_test_package_stamp"] = copy.deepcopy(
                receipt.get("stamp")
            )
        candidate["generation"] = int(holder.get("generation") or 0) + 1
        entry = ledger.build_wal_record(
            node_address=holder_address,
            event="contract_rebound",
            from_state=holder.get("state"),
            to_state=holder.get("state"),
            expected_generation=holder.get("generation"),
            generation=candidate["generation"],
            lease_epoch=holder.get("lease_epoch"),
            owner_token=holder.get("owner_token"),
            binding_delta={
                "contract_receipts": receipts,
                "contract_rebinds": rebinds,
                "contract_last_rebind_id": rebind_id,
                **compatibility_delta,
            },
            summary=(
                f"holder adopted current contract fingerprint for {contract_path} "
                f"through rebind {rebind_id}"
            ),
            artifacts=[marker, receipt.get("revision_record_ref")],
            seq=ledger.next_seq(),
        )
        _commit_binding_candidates({holder_address: candidate}, entry)
        return TransitionResult(ok=True, errors=[], warnings=[], binding=candidate)


def record_related_updates(
    primary_address: str,
    *,
    primary_delta: dict,
    related_deltas: Optional[dict[str, dict]] = None,
    expected_owner_tokens: Optional[dict[str, Optional[str]]] = None,
    event: str,
    summary: str,
    artifacts: Optional[list] = None,
) -> TransitionResult:
    """Commit state-preserving updates to several bindings as one replayable WAL intent.

    This is the control-plane companion to the message-answer transaction.  Contract
    ratification uses it where approval and current-version lineage must never split across
    owner/producer slices.
    """
    related_deltas = related_deltas or {}
    expected_owner_tokens = expected_owner_tokens or {}
    addresses = [primary_address, *related_deltas]
    with store.file_lock(_resolve_lock_path(), shared=False):
        bindings: dict[str, dict] = {}
        for address in addresses:
            binding = ledger.read_binding(address)
            if binding is None:
                return TransitionResult(
                    ok=False,
                    errors=[f"no binding for related update target {address!r}"],
                    warnings=[],
                    binding=bindings.get(primary_address),
                )
            expected = expected_owner_tokens.get(address)
            if expected is not None and binding.get("owner_token") != expected:
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"fencing abort (owner_token) on {event} for {address}: "
                        f"presented {expected!r} != live {binding.get('owner_token')!r}"
                    ],
                    warnings=[],
                    binding=bindings.get(primary_address) or binding,
                )
            bindings[address] = binding

        deltas = {primary_address: copy.deepcopy(primary_delta)}
        deltas.update({key: copy.deepcopy(value) for key, value in related_deltas.items()})
        candidates: dict[str, dict] = {}
        related: dict[str, dict] = {}
        for address, binding in bindings.items():
            candidate = copy.deepcopy(binding)
            candidate.update(deltas[address])
            candidate["node_address"] = address
            candidate["generation"] = int(binding.get("generation") or 0) + 1
            candidates[address] = candidate
            if address != primary_address:
                related[address] = {
                    "from_state": binding.get("state"),
                    "to_state": binding.get("state"),
                    "expected_generation": binding.get("generation"),
                    "generation": candidate["generation"],
                    "lease_epoch": binding.get("lease_epoch"),
                    "owner_token": binding.get("owner_token"),
                    "binding_delta": deltas[address],
                }

        primary = bindings[primary_address]
        primary_candidate = candidates[primary_address]
        entry = ledger.build_wal_record(
            node_address=primary_address,
            event=event,
            from_state=primary.get("state"),
            to_state=primary.get("state"),
            expected_generation=primary.get("generation"),
            generation=primary_candidate["generation"],
            lease_epoch=primary.get("lease_epoch"),
            owner_token=primary.get("owner_token"),
            binding_delta=deltas[primary_address],
            summary=summary,
            artifacts=[value for value in (artifacts or []) if value],
            seq=ledger.next_seq(),
            related_binding_deltas=related,
        )
        _commit_binding_candidates(candidates, entry)
        return TransitionResult(
            ok=True,
            errors=[],
            warnings=[],
            binding=primary_candidate,
        )


def withdraw_open_questions(
    sender_address: str,
    *,
    reason: str = "asker_terminal",
) -> TransitionResult:
    """Withdraw every still-open question on a terminal asker, with one loud WAL event."""
    with store.file_lock(_resolve_lock_path(), shared=False):
        binding = ledger.read_binding(sender_address)
        if binding is None:
            return TransitionResult(False, [f"no binding for {sender_address!r}"], [], None)
        messages = copy.deepcopy(binding.get("messages") or {})
        changed: list[str] = []
        for message_id, record in messages.items():
            if (
                isinstance(record, dict)
                and record.get("needs_answer")
                and record.get("question_state") == "open"
            ):
                record["question_state"] = "withdrawn"
                record["withdrawn_reason"] = reason
                record["withdrawn_at"] = clock.now_utc()
                changed.append(message_id)
        if not changed:
            return TransitionResult(True, [], [], binding)
        candidate = copy.deepcopy(binding)
        candidate["messages"] = messages
        candidate["generation"] = int(binding.get("generation") or 0) + 1
        entry = ledger.build_wal_record(
            node_address=sender_address,
            event="open_questions_withdrawn",
            from_state=binding.get("state"),
            to_state=binding.get("state"),
            expected_generation=binding.get("generation"),
            generation=candidate["generation"],
            lease_epoch=binding.get("lease_epoch"),
            owner_token=binding.get("owner_token"),
            binding_delta={"messages": messages},
            summary=(
                f"withdrew {len(changed)} open question(s) on terminal asker: "
                + ", ".join(changed)
            ),
            artifacts=[],
            seq=ledger.next_seq(),
        )
        _commit_binding_candidates({sender_address: candidate}, entry)
        return TransitionResult(True, [], [], candidate)


def _regenerate_handoff_packet(candidate_binding: dict, entry: dict) -> None:
    """Regenerate the DERIVED handoff (continuation/next-action) packet — THIN STUB in v1 (§4.4 step 3).

    The continuation packet is a derived artifact (regenerable from the committed WAL + binding),
    owned by a later increment (the chokepoint / reconcile handoff surface). v1 ships it as a
    documented no-op: the durable journal (WAL entry + binding checkpoint) is COMPLETE without it,
    and because the packet is purely derived, deferring it does not affect crash-replayability or the
    single-writer guarantee. NOTED to the orchestrator as an intentional stub.
    """
    # Intentionally a no-op in v1. The derived handoff packet is regenerated from committed state by
    # a later increment; nothing in the durable journal depends on it.
    return None


# ---------------------------------------------------------------------------
# journal() — the LOCKED direct-WAL-append primitive for non-transition journal rows (SWL-01).
# ---------------------------------------------------------------------------

def journal(
    node_address,
    *,
    event: str,
    from_state=None,
    to_state=None,
    lease_epoch=None,
    owner_token=None,
    binding_delta: Optional[dict] = None,
    summary: str = "",
    artifacts: Optional[list] = None,
) -> None:
    """Append ONE non-transition journal row to the run-ledger UNDER the per-mutation EX lock.

    SWL-01: the failure-path journal appends (spawn_failed escalations, *_send_failed,
    kickoff_*, delivery_failed_escalation, ipc_request_failed, …) used to run an UNLOCKED
    ``ledger.next_seq() + ledger.append_wal`` — racing the locked single writer across the
    daemon's two threads (IPC + poll), so two near-simultaneous allocations could both read
    ``max(seq)+1`` and append DUPLICATE seq values, breaking the FORK-SEQ global-monotonic
    contract the replay watermark and audit ordering key on (DAEMON §4.3: one serialization
    domain — "every mutation is a read→validate→commit fully inside the lock").

    This primitive owns the lock: seq allocation + append are one critical section. It writes
    NO binding (journal rows are not lifecycle transitions — ``expected_generation`` /
    ``generation`` are None so replay can never apply them). Callers must NOT already hold the
    EX lock (fcntl flock is not re-entrant); every journal site runs outside the executor's
    per-mutation acquire by construction.
    """
    with store.file_lock(_resolve_lock_path(), shared=False):
        record = ledger.build_wal_record(
            node_address=node_address,
            event=event,
            from_state=from_state,
            to_state=to_state,
            expected_generation=None,
            generation=None,
            lease_epoch=lease_epoch,
            owner_token=owner_token,
            binding_delta=binding_delta or {},
            summary=summary,
            artifacts=artifacts or [],
            seq=ledger.next_seq(),
        )
        ledger.append_wal(record)


# ---------------------------------------------------------------------------
# heartbeat() — own-slice liveness ping, owner_token REQUIRED (§4.5). Blind-overwrites its own
# slice under the lock but is fenced on owner_token so a stale owner cannot heartbeat over a live one.
# ---------------------------------------------------------------------------

def heartbeat(
    node_address: str,
    *,
    expected_owner_token: str,
    liveness_state: Optional[str] = None,
    last_heartbeat_at: Optional[str] = None,
    **_unused,
) -> TransitionResult:
    """Record a liveness ping into the node's OWN slice (§4.5). owner_token REQUIRED.

    A non-state-changing own-slice write (the lifecycle ``state`` is UNCHANGED): it refreshes
    ``last_heartbeat_at`` (and optionally ``liveness_state``) under the one EX lock, fenced on
    ``owner_token`` so a stale owner cannot heartbeat over a live one (the recovered ``cmd_heartbeat``
    blindly set ``owner`` with no epoch check — v1 requires the token on every mutator). Routes
    through ``_own_slice_write`` so the fence + lock discipline is shared with ``release_lease``.
    """
    delta: dict = {}
    if liveness_state is not None:
        delta["liveness_state"] = liveness_state
    if last_heartbeat_at is not None:
        delta["last_heartbeat_at"] = last_heartbeat_at
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta=delta,
        event="heartbeat",
        summary="liveness heartbeat (own-slice, fenced)",
    )


def release_lease(
    node_address: str,
    *,
    expected_owner_token: str,
    liveness_state: Optional[str] = None,
    **_unused,
) -> TransitionResult:
    """Release the lease on the node's OWN slice (§4.5). owner_token REQUIRED.

    A non-state-changing own-slice write fenced on ``owner_token``. The recovered
    ``cmd_release_lease`` (L1468) cleared the lease blindly; v1 requires the live token so a stale
    owner cannot release a re-spawned incarnation's lease. Clears ``last_heartbeat_at`` and marks the
    liveness slice (default ``idle``) without touching the lifecycle ``state``.
    """
    delta: dict = {"last_heartbeat_at": None}
    delta["liveness_state"] = liveness_state if liveness_state is not None else "idle"
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta=delta,
        event="release_lease",
        summary="lease released (own-slice, fenced)",
    )


def _own_slice_write(
    node_address: str,
    *,
    expected_owner_token: str,
    delta: dict,
    event: str,
    summary: str,
    skip_if_unchanged: bool = False,
    append_wal: bool = True,
    expected_fields: Optional[dict] = None,
) -> TransitionResult:
    """Shared own-slice write for heartbeat/release_lease: fence on owner_token, write under the lock.

    These are NOT lifecycle transitions (the ``state`` is unchanged), so they do NOT run the legality
    gate or bump the per-node generation — they blind-overwrite their OWN slice (§4.5). But they are
    fenced: a stale owner_token journals ``stale_return_ignored`` and leaves the binding UNCHANGED,
    exactly like the ``transition`` fencing precondition (non-destructive de-authorization).
    """
    with store.file_lock(_resolve_lock_path(), shared=False):
        binding = ledger.read_binding(node_address)
        if binding is None:
            return TransitionResult(
                ok=False,
                errors=[f"no binding for node {node_address!r}: cannot {event} an absent node"],
                warnings=[],
                binding=None,
            )

        # Fencing: owner_token required on a leased actor's own-slice write (§4.5) — a stale token is
        # journaled and the live binding left UNCHANGED. An UNFENCED control-plane own-slice write
        # (expected_owner_token=None — the deliver() promote path, where the daemon is the trusted
        # single writer and no actor presents a lease) skips the fence.
        if expected_owner_token is not None and binding["owner_token"] != expected_owner_token:
            fenced_entry = ledger.build_wal_record(
                node_address=node_address,
                event="stale_return_ignored",
                from_state=binding["state"],
                to_state=binding["state"],
                expected_generation=binding["generation"],
                generation=binding["generation"],
                lease_epoch=binding.get("lease_epoch"),
                owner_token=binding["owner_token"],
                binding_delta={"presented_owner_token": expected_owner_token, "attempted": event},
                summary=f"stale owner_token on {event}; de-authorized (non-destructive, §3.6)",
                artifacts=[],
                seq=ledger.next_seq(),
            )
            ledger.append_wal(fenced_entry)
            return TransitionResult(
                ok=False,
                errors=[
                    f"fencing abort (owner_token) on {event}: presented {expected_owner_token!r} "
                    f"!= live {binding['owner_token']!r} — journaled stale_return_ignored"
                ],
                warnings=[],
                binding=binding,
            )

        for key, expected in (expected_fields or {}).items():
            if binding.get(key) != expected:
                return TransitionResult(
                    ok=False,
                    errors=[
                        f"{event} precondition failed for {key}: expected {expected!r}, "
                        f"live {binding.get(key)!r}"
                    ],
                    warnings=[],
                    binding=binding,
                )

        if skip_if_unchanged and all(binding.get(key) == value for key, value in delta.items()):
            return TransitionResult(ok=True, errors=[], warnings=[], binding=binding)

        return _commit_own_slice_locked(
            binding,
            delta=delta,
            event=event,
            summary=summary,
            append_wal=append_wal,
        )


def _commit_own_slice_locked(
    binding: dict,
    *,
    delta: dict,
    event: str,
    summary: str,
    append_wal: bool = True,
) -> TransitionResult:
    """Commit one already-authorized own-slice mutation under the held EX lock."""
    node_address = binding["node_address"]
    candidate = copy.deepcopy(binding)
    candidate.update(delta)
    if append_wal:
        # Replay-changing own-slice writes carry a NON-state-changing WAL row (state unchanged,
        # generation unchanged). Replay-neutral observations keep their durable node-local home
        # and only refresh this derived binding checkpoint.
        entry = ledger.build_wal_record(
            node_address=node_address,
            event=event,
            from_state=binding["state"],
            to_state=binding["state"],
            expected_generation=binding["generation"],
            generation=binding["generation"],  # unchanged — not a CAS-advancing transition
            lease_epoch=candidate.get("lease_epoch"),
            owner_token=candidate.get("owner_token"),
            binding_delta=delta,
            summary=summary,
            artifacts=[],
            seq=ledger.next_seq(),
        )
        candidate["last_applied_seq"] = entry["seq"]
        ledger.append_wal(entry)
    whole_map = ledger.all_nodes()
    whole_map[node_address] = candidate
    ledger.write_binding(whole_map, _lock_held=True)
    return TransitionResult(ok=True, errors=[], warnings=[], binding=candidate)


def turn_state_checkpoint(
    node_address: str,
    *,
    delta: dict,
    expected_owner_token: str,
    event: str = "turn_state_observed",
    summary: str = "runtime hook turn state observed",
) -> TransitionResult:
    """Adopt node-local hook state through the single writer, edge-triggered.

    Ordinary observations already live durably in the node's turn-event file, so replaying a
    duplicate run-ledger row changes no rebuilt state. Health-edge events remain journaled because
    their replay does change the detector fallback checkpoint.
    """
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta=dict(delta),
        event=event,
        summary=summary,
        skip_if_unchanged=True,
        append_wal=event != "turn_state_observed",
    )


def record_seat_stall(
    node_address: str,
    *,
    expected_owner_token: str,
    incident_id: str,
    detected_at: str,
    classification: str,
    silent_seconds: float,
    pane_excerpt: str,
    prompt_signature: Optional[str],
    cancel_status: str,
    positive_incident_count: int,
    retriggered: bool,
    escalated: bool,
    root_limit: bool = False,
) -> TransitionResult:
    """Write one replayable blocked-input incident intent before any keystroke."""
    delta = {
        "seat_stall_active": True,
        "seat_stall_incident_id": str(incident_id),
        "seat_stall_since": str(detected_at),
        "seat_stall_classification": str(classification),
        "seat_stall_silent_seconds": float(silent_seconds),
        "seat_stall_pane_excerpt": str(pane_excerpt),
        "seat_stall_prompt_signature": prompt_signature,
        "seat_stall_cancel_status": str(cancel_status),
        "seat_stall_positive_incident_count": int(positive_incident_count),
        "seat_stall_retriggered": bool(retriggered),
        "seat_stall_escalated": bool(escalated),
        "seat_stall_root_limit": bool(root_limit),
        "seat_stall_recovered_at": None,
        "seat_stall_recovery_reason": None,
    }
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta=delta,
        event="seat_stalled",
        summary=(
            f"seat stalled: {classification} after {float(silent_seconds):.1f}s silent; "
            f"cancel={cancel_status} retriggered={bool(retriggered)} "
            f"escalated={bool(escalated)}"
        ),
    )


def record_seat_stall_action(
    node_address: str,
    *,
    expected_owner_token: str,
    incident_id: str,
    actioned_at: str,
    cancel_status: str,
) -> TransitionResult:
    """Record the observed result of the one write-ahead Escape attempt."""
    if cancel_status not in {"sent", "failed"}:
        raise ValueError("seat stall action status must be 'sent' or 'failed'")
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        expected_fields={
            "seat_stall_active": True,
            "seat_stall_incident_id": str(incident_id),
            "seat_stall_cancel_status": "pending",
        },
        delta={
            "seat_stall_incident_id": str(incident_id),
            "seat_stall_cancel_status": cancel_status,
            "seat_stall_actioned_at": str(actioned_at),
        },
        event="seat_stall_actioned",
        summary=(
            f"seat stall cancellation attempt {incident_id}: observed result={cancel_status}"
        ),
    )


def recover_seat_stall(
    node_address: str,
    *,
    expected_owner_token: str,
    incident_id: str,
    recovered_at: str,
    reason: str,
) -> TransitionResult:
    """Clear only the active incident flag after renewed life evidence."""
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        expected_fields={
            "seat_stall_active": True,
            "seat_stall_incident_id": str(incident_id),
        },
        delta={
            "seat_stall_active": False,
            "seat_stall_incident_id": str(incident_id),
            "seat_stall_recovered_at": str(recovered_at),
            "seat_stall_recovery_reason": str(reason),
        },
        event="seat_stall_recovered",
        summary=f"seat stall recovered: {reason}",
    )


# ---------------------------------------------------------------------------
# deliver() — the deliverable-block write (§3.2 deliverable_state / delivery_destination), routed
# through the SINGLE writer. Increment 17 (control-plane promotion) calls this — and ONLY this — to
# record a delivery outcome on the binding; promote.py NEVER touches ledger.write_binding directly.
#
# Shape: an own-slice-style write (like heartbeat/release_lease). The deliverable block lives ALONGSIDE
# the lifecycle `state` (§3.2: state | deliverable_state are distinct axes); a promote does NOT advance
# the lifecycle state (the accepted project stays `done` — a terminal lifecycle state), it only stamps
# the deliverable block. So this is deliberately NOT a lifecycle transition(): it runs no legality gate
# (a `done` node has no legal forward edge — a transition() would abort) and bumps no per-node
# generation. It IS the single writer: it journals a WAL row (actor='harnessd', hard-coded by the
# ledger) and advances last_applied_seq via commit-equivalent intent-first ordering, so the delivery is
# audited like every other state change and is attributable to harnessd, never a jailed agent.
# ---------------------------------------------------------------------------

def deliver(
    node_address: str,
    *,
    deliverable_state: str,
    delivery_destination: Optional[str] = None,
    delivery_source: Optional[str] = None,
    extra_delta: Optional[dict] = None,
    expected_owner_token: Optional[str] = None,
    event: str,
    summary: str,
) -> TransitionResult:
    """Record a deliverable-block outcome (§3.2) via the single writer — own-slice, fenced-when-asked.

    Writes ``deliverable_state`` (and, when given, ``delivery_destination`` / ``delivery_source`` /
    ``extra_delta``) into the node's OWN slice under the one EX lock, journaling a WAL row
    (``actor='harnessd'``) and advancing ``last_applied_seq`` (intent-first ordering, §4.4). The
    lifecycle ``state`` is UNCHANGED — a delivery is orthogonal to the lifecycle axis (§3.2), so no
    legality gate runs and no generation bumps (mirrors the own-slice discipline of
    ``heartbeat``/``release_lease``). ``write_targets`` is NEVER touched here: the out-of-jail
    destination belongs ONLY in ``delivery_destination`` (DAEMON §3.2 — distinct fields, jail
    boundary legible), and ``delivery_source`` records the selected in-jail product surface.

    Fencing is OPTIONAL: the control plane (the promote op) drives this, not a jailed actor presenting a
    lease. When ``expected_owner_token`` is given it fences exactly like the other mutators (a stale token
    journals ``stale_return_ignored`` and leaves the binding UNCHANGED); when None it is an unfenced
    control-plane write (the daemon is the trusted single writer — §4.3).
    """
    delta: dict = {"deliverable_state": deliverable_state}
    if delivery_destination is not None:
        delta["delivery_destination"] = delivery_destination
    if delivery_source is not None:
        delta["delivery_source"] = delivery_source
    if extra_delta:
        delta.update(dict(extra_delta))
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta=delta,
        event=event,
        summary=summary,
    )


def record_admission(
    node_address: str,
    *,
    delta: dict,
    expected_owner_token: Optional[str] = None,
    event: str = "admission_updated",
    summary: str = "admission state updated",
) -> TransitionResult:
    """Record scheduler/admission metadata through the single writer.

    Admission is control-plane state, not a lifecycle transition: a planned node can be accepted into
    the ledger while waiting for a predecessor to pass its gate. This journals that fact without
    opening an actor and without changing the node's lifecycle state.
    """
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta=dict(delta),
        event=event,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# pause() / resume() / post_answer() — the F16 human-control WRITE verbs (TRANSPORTS §5.3),
# routed through the SINGLE writer. DAEMON §3.2: paused_at is "set/cleared ONLY by the human
# control surface, routed through the single-writer executor — never raw." These are own-slice
# CONTROL-PLANE writes, NOT lifecycle transitions: the lifecycle `state` is unchanged, no
# legality gate runs, no generation bumps (the deliver()/heartbeat discipline — pause is
# orthogonal to the lifecycle axis). expected_owner_token=None is the established UNFENCED
# control-plane posture (deliver()): the human holds no agent lease. Each write journals a WAL
# row (audit) and advances last_applied_seq intent-first.
# ---------------------------------------------------------------------------

def pause(
    node_address: str,
    *,
    paused_at: Optional[str] = None,
    expected_owner_token: Optional[str] = None,
) -> TransitionResult:
    """Set ``paused_at`` — pause the SUBTREE rooted here (TRANSPORTS §5.3 primitive 1).

    A FLAG the spawner and watchdog respect, NOT a kill: the in-flight agent keeps running.
    What stops: (a) admission of new children (chokepoint STEP0) and (b) all watchdog recovery
    actions (WATCHDOG §3.4 STEP 0). The agent's own fenced terminal sign-off is still honored.
    """
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta={"paused_at": paused_at or clock.now_utc()},
        event="paused",
        summary=(
            "human pause: paused_at set — subtree admits no child, watchdog skips recovery "
            "(TRANSPORTS §5.3 primitive 1)"
        ),
    )


def resume(
    node_address: str,
    *,
    expected_owner_token: Optional[str] = None,
) -> TransitionResult:
    """Clear ``paused_at`` — re-admit children + recovery for the subtree rooted here."""
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta={"paused_at": None},
        event="resumed",
        summary="human resume: paused_at cleared — children + watchdog recovery re-admitted",
    )


def post_answer(
    node_address: str,
    *,
    answer: str,
    expected_owner_token: Optional[str] = None,
    route: str = "human_to_parent",
    actor: Optional[str] = None,
    artifact: Optional[str] = None,
    signal_artifact_seen_at: Optional[str] = None,
    event: str = "human_answer_posted",
    summary: Optional[str] = None,
) -> TransitionResult:
    """Stamp canonical answered state for an ESCALATED slot.

    The answer RIDES terminal_signal=ESCALATED + terminal_note — this stamps the note ALONGSIDE
    the held ESCALATED signal (which is deliberately NOT cleared here: the parent reads both;
    clearing belongs to the round-trip completion). The ESCALATED guard and route-specific wake
    hop live in the IPC handler; this is only the durable single-writer stamp.

    ``answered_at`` is stamped too (SM-4): it is the EXPIRY instant for the slot-hold's
    legit-waiting reason — an escalation whose answer is fresher than its question
    (``detector_signals.escalation_answered``) no longer shields the node from the idle ladder,
    and a LATER question (a fresh harness-observed artifact identity / a re-journaled
    terminal_signal_at) re-arms it.
    Without this, a node that ever escalated could never read 'idle' again and a second
    escalation could never journal.
    """
    delta = {
        "terminal_note": answer,
        "answered_at": clock.now_utc(),
        "answer_route": route,
    }
    if actor:
        delta["answer_actor"] = actor
    if artifact:
        delta["answer_artifact"] = artifact
    if signal_artifact_seen_at:
        delta["answered_signal_artifact_seen_at"] = signal_artifact_seen_at
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta=delta,
        event=event,
        summary=summary or (
            "human answer posted into the ESCALATED slot (terminal_note + answered_at; "
            "TRANSPORTS §5.3 primitive 3)"
        ),
    )


def ack_inbox(
    node_address: str,
    *,
    acked_offset: int,
    expected_owner_token: Optional[str] = None,
) -> TransitionResult:
    """Advance ``last_inbox_acked_offset`` — the ③-wake edge-trigger watermark (TRANSPORTS §2).

    The daemon's wake resolver calls this AFTER a delivered nudge is VERIFIED CONSUMED (R-1 /
    TRANSPORTS §3.2 P3: the transcript JSONL grew past the at-send baseline — a NEW turn): the
    watermark moves to the nudge's pre-send inbox size so the SAME line is never re-nudged on
    the next poll (``inbox_has_unacked`` is edge-triggered — one nudge per NEW line, no per-poll
    storm). A suppressed/failed/UNVERIFIED send deliberately does NOT ack, so the next tick
    retries. The ack CLEARS the pending-verification record (``wake_pending_ack_offset`` /
    ``wake_sent_transcript_size``) in the SAME atomic write — a resolved pending can never be
    re-resolved (no double-ack).

    An own-slice CONTROL-PLANE write (the deliver()/pause() discipline): the lifecycle ``state``
    is unchanged, no legality gate runs, no generation bumps; one WAL row journals the ack.
    ``expected_owner_token=None`` is the daemon-internal unfenced posture (the EX lock is the
    serialization) — the daemon, not an agent lease, owns the wake watermark.
    """
    return _own_slice_write(
        node_address,
        expected_owner_token=expected_owner_token,
        delta={
            "last_inbox_acked_offset": int(acked_offset),
            "wake_pending_ack_offset": None,
            "wake_sent_transcript_size": None,
            "wake_attempt_count": 0,
            "wake_cap_emitted_at": None,
        },
        event="inbox_acked",
        summary=(
            f"③-wake verified consumed: last_inbox_acked_offset -> {int(acked_offset)} "
            "(edge-trigger watermark; pending verification and wake budget cleared in the same write)"
        ),
    )


def record_wake_pending(
    node_address: str,
    *,
    pending_ack_offset: int,
    sent_transcript_size: int,
    expected_owner_token: Optional[str] = None,
) -> TransitionResult:
    """Record a DELIVERED-but-unverified wake/kickoff nudge (R-1: verify-new-turn, TRANSPORTS §3.2 P3).

    send-keys rc=0 only proves tmux accepted the bytes — NOT that the agent consumed them (an
    operator's interactive attach in copy-mode eats the keystrokes while capture-pane still shows
    the idle prompt and send-keys still exits 0). So a delivered send does NOT ack the inbox
    watermark; it records the pending verification on the binding:

      * ``wake_pending_ack_offset`` — the PRE-send inbox size (the SWL-03 ack target: lines
        appended mid-send stay above it and re-trigger);
      * ``wake_sent_transcript_size`` — the transcript JSONL byte size at send time (the
        verify-new-turn growth baseline; an absent transcript records 0).
      * ``wake_attempt_count`` — consecutive delivered wakes while the durable inbox receipt
        remains fixed. Attempt three emits ``wake_cap`` and no fourth send is allowed.

    The daemon's wake resolver acks to the recorded offset ONLY after the transcript grows past
    the baseline (``watchdog.confirm_prod_worked``); until then the unacked watermark keeps the
    retry armed up to the cap, and a re-send OVERWRITES this record with ITS covered offset
    (one pending verification at a time per node). The attempt increment is derived under the
    same EX lock as the write; only ``ack_inbox`` clears it.
    """
    with store.file_lock(_resolve_lock_path(), shared=False):
        binding = ledger.read_binding(node_address)
        if binding is None:
            return TransitionResult(
                ok=False,
                errors=[
                    f"no binding for node {node_address!r}: cannot record a wake for an absent node"
                ],
                warnings=[],
                binding=None,
            )
        if (
            expected_owner_token is not None
            and binding.get("owner_token") != expected_owner_token
        ):
            fenced_entry = ledger.build_wal_record(
                node_address=node_address,
                event="stale_return_ignored",
                from_state=binding["state"],
                to_state=binding["state"],
                expected_generation=binding["generation"],
                generation=binding["generation"],
                lease_epoch=binding.get("lease_epoch"),
                owner_token=binding.get("owner_token"),
                binding_delta={
                    "presented_owner_token": expected_owner_token,
                    "attempted": "wake_verify_pending",
                },
                summary=(
                    "stale owner_token on wake_verify_pending; de-authorized "
                    "(non-destructive, §3.6)"
                ),
                artifacts=[],
                seq=ledger.next_seq(),
            )
            ledger.append_wal(fenced_entry)
            return TransitionResult(
                ok=False,
                errors=[
                    f"fencing abort (owner_token) on wake_verify_pending: presented "
                    f"{expected_owner_token!r} != live {binding.get('owner_token')!r}"
                ],
                warnings=[],
                binding=binding,
            )

        prior_attempts = int(binding.get("wake_attempt_count") or 0)
        if prior_attempts >= 3:
            return TransitionResult(ok=True, errors=[], warnings=[], binding=binding)
        attempt = prior_attempts + 1
        delta = {
            "wake_pending_ack_offset": int(pending_ack_offset),
            "wake_sent_transcript_size": int(sent_transcript_size),
            "wake_attempt_count": attempt,
        }
        if attempt == 3:
            delta["wake_cap_emitted_at"] = clock.now_utc()
            event = "wake_cap"
            summary = (
                f"wake notification cap reached for {node_address}: 3 delivered wakes for "
                f"the still-unconsumed receipt at offset "
                f"{int(binding.get('last_inbox_acked_offset') or 0)}; pending verification "
                "remains armed, no fourth wake will be sent, and watchdog health owns the next move"
            )
        else:
            event = "wake_verify_pending"
            summary = (
                f"wake nudge {attempt}/3 delivered (rc=0) — ack DEFERRED pending "
                f"verify-new-turn: ack to {int(pending_ack_offset)} once the transcript grows "
                f"past {int(sent_transcript_size)} bytes (R-1; TRANSPORTS §3.2 P3)"
            )
        return _commit_own_slice_locked(
            binding,
            delta=delta,
            event=event,
            summary=summary,
        )


# ---------------------------------------------------------------------------
# watchdog_checkpoint() — own-slice liveness write + ONE run-ledger row, EDGE-TRIGGERED (§4.5 /
# WATCHDOG §3.5). No append on a steady-healthy poll; resets the stale-counter on a healthy observation.
# ---------------------------------------------------------------------------

def watchdog_checkpoint(
    node_address: str,
    *,
    condition: str,
    liveness_state: str,
    last_progress_at,
    last_evidence,
    expected_owner_token: Optional[str],
    gate_crossed_at=None,
) -> CheckpointResult:
    """Write the watchdog's own-slice liveness observation + ONE edge-triggered run-ledger row.

    EDGE-TRIGGERED (WATCHDOG §3.5): a steady-healthy poll (the observed ``condition``/``liveness_state``
    already match the live slice and the observation is healthy) writes NO WAL row — only a changed
    observation (an edge) appends. A HEALTHY observation RESETS the stale-counter (``stale_check_count``
    -> 0); the recovered model reset it on healthy (L1337-1350) and that reset is preserved here.

    Fenced on ``expected_owner_token`` when presented (§4.5). The lifecycle ``state`` is NOT changed
    (this is an observation, not a transition); generation is not bumped.
    """
    with store.file_lock(_resolve_lock_path(), shared=False):
        binding = ledger.read_binding(node_address)
        if binding is None:
            return CheckpointResult(
                ok=False,
                appended=False,
                binding=None,
                errors=[f"no binding for node {node_address!r}: cannot checkpoint an absent node"],
            )

        if expected_owner_token is not None and binding["owner_token"] != expected_owner_token:
            fenced_entry = ledger.build_wal_record(
                node_address=node_address,
                event="stale_return_ignored",
                from_state=binding["state"],
                to_state=binding["state"],
                expected_generation=binding["generation"],
                generation=binding["generation"],
                lease_epoch=binding.get("lease_epoch"),
                owner_token=binding["owner_token"],
                binding_delta={"presented_owner_token": expected_owner_token, "attempted": "watchdog_checkpoint"},
                summary="stale owner_token on watchdog_checkpoint; de-authorized (non-destructive, §3.6)",
                artifacts=[],
                seq=ledger.next_seq(),
            )
            ledger.append_wal(fenced_entry)
            return CheckpointResult(
                ok=False,
                appended=False,
                binding=binding,
                errors=[
                    f"fencing abort (owner_token) on watchdog_checkpoint: presented "
                    f"{expected_owner_token!r} != live {binding['owner_token']!r}"
                ],
            )

        healthy = liveness_state in ("working", "waiting")

        # Edge detection: the observation is an EDGE iff it changes the recorded liveness slice
        # (condition / liveness_state / last_progress_at), OR it must reset a non-zero stale counter.
        prior_stale = binding.get("stale_check_count", 0)
        slice_changed = (
            binding.get("condition") != condition
            or binding.get("liveness_state") != liveness_state
            or binding.get("last_progress_at") != last_progress_at
        )
        must_reset_counter = healthy and prior_stale != 0
        # An IDLE observation ADVANCES the two-counter ladder (derive_checkpoint: stale_count+1 on a
        # STALE_FAMILY condition, L206-211) — without this the counter never grows and the
        # idle->prod->FAILED ladder can never reach grace (the leaf would be prodded forever).
        must_bump_counter = liveness_state == "idle"

        if not slice_changed and not must_reset_counter and not must_bump_counter:
            # Steady-healthy poll: nothing changed and no counter to reset/bump -> NO WAL append.
            return CheckpointResult(ok=True, appended=False, binding=binding, errors=[])

        checkpoint_delta = {
            "condition": condition,
            "liveness_state": liveness_state,
            "last_progress_at": last_progress_at,
            "last_evidence": last_evidence,
        }
        if gate_crossed_at is not None:
            checkpoint_delta["gate_crossed_at"] = gate_crossed_at
        # The two-counter discipline (§3.5 / WATCHDOG §3.5): a HEALTHY observation RESETS the
        # stale-counter to 0 (L1337-1350); an IDLE observation INCREMENTS it (the ladder rung that
        # eventually crosses stale_grace_checks -> FAILED). Other states leave it untouched.
        if healthy:
            checkpoint_delta["stale_check_count"] = 0
        elif liveness_state == "idle":
            checkpoint_delta["stale_check_count"] = (prior_stale or 0) + 1
        candidate = copy.deepcopy(binding)
        candidate.update(checkpoint_delta)

        entry = ledger.build_wal_record(
            node_address=node_address,
            event="watchdog_checkpoint",
            from_state=binding["state"],
            to_state=binding["state"],
            expected_generation=binding["generation"],
            generation=binding["generation"],  # unchanged — an observation, not a transition
            lease_epoch=candidate.get("lease_epoch"),
            owner_token=candidate.get("owner_token"),
            binding_delta=checkpoint_delta,
            summary=f"watchdog observation: condition={condition} liveness={liveness_state}",
            artifacts=[],
            seq=ledger.next_seq(),
        )
        candidate["last_applied_seq"] = entry["seq"]
        ledger.append_wal(entry)
        whole_map = ledger.all_nodes()
        whole_map[node_address] = candidate
        ledger.write_binding(whole_map, _lock_held=True)
        return CheckpointResult(ok=True, appended=True, binding=candidate, errors=[])
