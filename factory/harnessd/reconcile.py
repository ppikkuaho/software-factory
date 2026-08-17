"""The tmux<->ledger reconcile sweep — WAL replay + the on-restart classification (§5.1/§5.2).

The crash-recovery half of the daemon. Intent-first commit (DAEMON §4.4) appends+fsyncs the WAL
event BEFORE the binding atomic-replace, so a crash between the two leaves the WAL AHEAD of the
binding. Recovery therefore (1) deterministically REPLAYS any committed-but-not-yet-checkpointed WAL
event (``replay_wal``), then (2) classifies every binding against live tmux (the §5.1 five branches,
``reconcile_on_restart``). The continuous sweep (``reconcile_tick``) re-derives liveness via the
detector and applies the same resolutions, edge-triggered.

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.10 (the frozen reconcile.py interface — exact signatures below),
    §2.13 (the ``ReconcileReport`` result type).
  - DAEMON §4.4 (intent-first ordering + the replay watermark/pre-image rule), §5.1
    (reconcile-on-restart: the five-branch sweep), §5.2 (continuous reconciliation), §5.4 (the two
    reapers — leaf-reap vs coordinator-detect-and-escalate; the recover-vs-reap CHOICE is cluster-②,
    NOT decided here).

SINGLE-WRITER DISCIPLINE (DAEMON §4.3, the load-bearing constraint): every binding MUTATION in
``reconcile_on_restart`` / ``reconcile_tick`` routes through the REAL ``executor`` (the one writer —
``executor.transition``), never a raw ``ledger.write_binding`` poke. ``replay_wal`` is the ONE
exception, and it is spec-sanctioned (§2.10): the boot-replay checkpoint ACQUIRES the per-mutation
EX lock (``store.file_lock(executor.lock_path(), shared=False)``) around its read-replay-write
critical section — so ``write_binding(..., _lock_held=True)`` is true-BY-FACT, the cross-node-
clobber hazard cannot occur (F6/reconcile-2/SWCAS-01), and the whole map lands as ONE recovery
checkpoint per node (FORK-REPLAY — one atomic-replace per node, not one per event). The lock
releases before the classification sweep, whose mutations re-take it per transition.

RESOLVED DETAILS (unspecified by the frozen tests; decided spec-faithfully, surfaced to the
orchestrator):

  * LEAF-vs-COORD DISCRIMINATOR (§5.4 "a dead leaf … vs a dead coordinator"). A binding is a
    COORDINATOR iff ANOTHER binding in the live map names it as ``parent_address`` (i.e. it has at
    least one descendant seat). Otherwise it is a LEAF. The denormalized ``parent_address`` field is
    the §3.1-sanctioned reconcile-speed pointer ("a denormalized ``parent`` field MAY be stored to
    speed reconcile sweeps"); we fall back to address-prefix arithmetic (§3.1 "topology is derivable
    by prefix arithmetic": a child address is ``<parent-path>/<seg>#<seat>``) when a binding carries
    no ``parent_address``, so the discriminator is correct even on a map seeded without the
    denormalized pointer. This is the §5.4 mechanism-level coordinator/leaf asymmetry cluster-① owns.

  * died_* CLASS for a leaf-necro (§3.6 death class). A reconcile-detected dead leaf is an
    INFRASTRUCTURE death (the pane is gone / pane_dead — the session died, not a methodology
    failure), so we stamp ``terminal_signal = "DIED_INFRA"`` (the §3.6 SCREAMING binding value;
    ``states.TERMINAL_VOCAB["died_infrastructure"].terminal_signal``). The run-ledger event is the
    snake ``died_infrastructure`` and the lifecycle state is the lowercase ``failed`` (the §3.6
    spelling split — all three layers sourced from the SAME TERMINAL_VOCAB row, review reconcile-1).
    DIED_METHODOLOGY is reserved for an agent-classified methodology give-up, not a process-death
    the sweep observes.

  * coordinator_died STAMP. The §3.6 ``coordinator_died`` row has ``terminal_signal=None`` (a
    coordinator death is daemon-stamped and NOT collapsed — "recovered-as-orphan", §5.4), state
    ``dead``, and the run-ledger ``event = "coordinator_died"``. The code follows the row exactly:
    the coordinator necro passes ``terminal_signal=None`` (NO binding terminal_signal stamp — the
    death class lives in the EVENT + the escalation ``kind``, preserving the §3.6 spelling split;
    asserted by the coordinator-died test). This is the DELIBERATE asymmetry against the leaf leg:
    leaf DIED_INFRA → state ``failed`` + terminal_signal stamped; coordinator → state ``dead`` +
    terminal_signal None.

  * THE ``ReconcileReport`` SHAPE = the §2.13 frozen ``@dataclass(frozen=True)`` exactly:
    ``adopted: list[str]; necroed: list[str]; escalations: list[dict]``. ``adopted`` / ``necroed``
    are lists of node_address strings; ``escalations`` is a list of dicts, each carrying at least a
    ``node_address`` key (the §2.13 "escalations is a list of dicts"), plus a ``reason`` and a
    ``kind`` so cluster-②'s escalation handler can route it (orphan vs coordinator_died) without
    re-deriving the cause. For an ORPHAN (no binding) the ``node_address`` is the alive-but-unowned
    ``tmux_target`` (there is no node_address — the target IS the only identity the sweep has), and a
    ``tmux_target`` key carries it explicitly too.

  * RESUME-NOT-DOUBLE-SPAWN L1 (§5.1 step 5). ``reconcile_on_restart`` NEVER spawns. It classifies
    the L1 binding by the SAME five branches as any node (adopt if its pane is live+uuid-matched;
    necro/escalate if its pane is gone) and records the verdict in the report. The actual
    spawn-or-resume decision for L1 is genesis's job (DAEMON §7 / IMPLEMENTATION-PLAN §2.12
    ``run_genesis``: "reconcile_on_restart -> if no live non-terminal L1 binding: spawn … else
    RESUME"); reconcile provides the classification genesis reads, and by NEVER spawning here the
    "no double-spawn" invariant (F35) holds structurally — there is no spawn code path in this
    module to double-fire.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# NB: ``executor`` is imported for ``lock_path()`` ONLY (the §4.3 per-mutation lock the replay
# checkpoint takes; no cycle — executor does not import reconcile). The sweep itself never calls
# the module directly: every mutation routes through the INJECTED ``executor`` argument.
from . import executor as _executor_mod
from . import fencing, ledger, states, store, wal_policy


# ---------------------------------------------------------------------------
# Result type (§2.13 frozen dataclass).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReconcileReport:
    """The outcome of a reconcile sweep (§2.13).

    ``adopted``     — node_addresses whose live pane was adopted (ownership resumed; §5.1 branch 1).
    ``necroed``     — node_addresses driven to their §3.6 death-class state by an owned-but-dead
                      leaf-necro (``failed`` for a leaf DIED_INFRA; §5.1 branch 2). Only a COMMITTED
                      necro lands here — an aborted terminal write surfaces in ``escalations``
                      (kind ``necro_failed``) instead.
    ``escalations`` — a list of dicts (§2.13), one per ambiguous case handed to cluster-②: a dead
                      COORDINATOR (recover-vs-reap is ②, §5.4), an alive-but-unowned ORPHAN (§5.1
                      branch 5), and an aborted leaf-necro write (``necro_failed``). Each dict carries
                      at least ``node_address``; orphans also carry ``tmux_target``.
    """

    adopted: list = field(default_factory=list)
    necroed: list = field(default_factory=list)
    escalations: list = field(default_factory=list)


# The §3.6 SCREAMING binding value a reconcile-detected dead leaf is stamped with (infrastructure
# death — the pane is gone, not an agent-classified methodology give-up). Sourced from the canonical
# §3.6 vocabulary so the spelling cannot drift from states.TERMINAL_VOCAB. The lifecycle STATE layer is
# sourced from the SAME row (review reconcile-1): DIED_INFRA resolves to state "failed", per §3.6.
_DIED_INFRA_SIGNAL: str = states.TERMINAL_VOCAB["died_infrastructure"].terminal_signal
_DIED_INFRA_EVENT: str = states.TERMINAL_VOCAB["died_infrastructure"].event
_DIED_INFRA_STATE: str = states.TERMINAL_VOCAB["died_infrastructure"].state  # "failed" (§3.6)

# The coordinator-death class (§3.6 / §5.4). Per the §3.6 TERMINAL_VOCAB row, coordinator_died carries
# the run-ledger EVENT "coordinator_died", state "dead", and terminal_signal **None** — the death class
# lives in the EVENT + the escalation's `kind`, NOT as a binding terminal_signal (the valid
# terminal_signal values are DONE|FAILED|ESCALATED|DIED_INFRA|DIED_METHODOLOGY|FENCED; "coordinator_died"
# is an event name, and stamping it as a terminal_signal would violate the §3.6 spelling-split). So the
# coordinator necro passes terminal_signal=None (state -> dead, event appended, no terminal_signal stamp).
_COORDINATOR_DIED_EVENT: str = states.TERMINAL_VOCAB["coordinator_died"].event
_COORDINATOR_DIED_SIGNAL = states.TERMINAL_VOCAB["coordinator_died"].terminal_signal  # None (per §3.6)
_COORDINATOR_DIED_STATE: str = states.TERMINAL_VOCAB["coordinator_died"].state  # "dead" (§3.6)


# ===========================================================================
# PART A — replay_wal: deterministic + idempotent re-apply with the pre-image CAS.
# ===========================================================================

def binding_effects_by_node(wal: list) -> dict:
    """Project every WAL row into the binding effects recovery actually replays.

    A multi-binding commit owns one primary effect plus zero or more
    ``related_binding_deltas`` at the same sequence.  Recovery and committed
    validation must consume this one projection so neither can silently omit a
    binding checkpoint the single writer advanced.
    """
    effects_by_node: dict = {}
    for event in wal:
        effects_by_node.setdefault(event["node_address"], []).append(event)
        for related_address, related in (event.get("related_binding_deltas") or {}).items():
            if not isinstance(related, dict):
                continue
            # Project a related slice into the same ordinary replay shape. It retains the original
            # seq, so both halves of the one intent share one replay/audit identity.
            effects_by_node.setdefault(related_address, []).append(
                {
                    "seq": event["seq"],
                    "node_address": related_address,
                    "event": event.get("event"),
                    "actor": event.get("actor"),
                    "from_state": related.get("from_state"),
                    "to_state": related.get("to_state"),
                    "expected_generation": related.get("expected_generation"),
                    "generation": related.get("generation"),
                    "lease_epoch": related.get("lease_epoch"),
                    "owner_token": related.get("owner_token"),
                    "binding_delta": related.get("binding_delta") or {},
                }
            )
    return effects_by_node


def replay_wal(bindings: dict, wal: list) -> dict:
    """Deterministically replay committed-but-not-checkpointed WAL events (§2.10 / §4.4).

    For each event with ``seq > binding.last_applied_seq`` (the per-node replay watermark), first
    require the single ``wal_policy`` authority to classify it as a canonical binding replay:

      * ``event.expected_generation == event.generation`` and the live generation matches — this is
        a generation-preserving own-slice row. The watermark is its landed/pending discriminator:
        because the row is above the watermark, APPLY its ``binding_delta`` and advance the
        watermark.
      * otherwise, ``binding.generation == event.expected_generation`` — the CAS PRE-IMAGE matches:
        APPLY the ``binding_delta``, then set ``state`` to the record's authoritative ``to_state``
        and ``node_address`` to the record's ``node_address`` (authoritative over the delta — the
        mirror of ``executor.transition``'s candidate rule), set ``generation`` + ``owner_token`` to
        the event's POST-commit values, and stamp ``last_applied_seq = event.seq``.
      * otherwise, ``binding.generation == event.generation`` — the generation-advancing event
        ALREADY LANDED (the binding atomic-replace did commit before the crash): NO-OP skip.
      * a replay-neutral or unclassified event, or neither generation case — do NOT apply it. This
        prevents journal-only attribution deltas from becoming binding state and prevents a
        wrong-pre-image transition from corrupting the checkpoint. The watermark is not advanced.

    An event with ``seq <= last_applied_seq`` is below the watermark and is skipped entirely.

    FORK-REPLAY (recovery-checkpoint atomicity): all pending events for a node are BATCHED and applied
    in ``seq`` order into ONE result binding (one recovery checkpoint per node, not one per event).
    Deterministic + idempotent: replaying the same WAL twice yields the same map (a landed event is a
    no-op on the second pass because its post-commit generation now equals the live generation).

    PURE over the in-memory ``bindings`` map: returns a NEW map (the inputs are not mutated). The
    on-disk write of the replayed map is the caller's job (``reconcile_on_restart`` writes it via
    ``ledger.write_binding(..., _lock_held=True)`` inside the per-mutation EX lock it ACQUIRES for
    the whole read-replay-write checkpoint — §2.10, F6/reconcile-2).
    """
    # Group pending events by node, so each node's events apply as one in-seq-order chain (FORK-REPLAY).
    events_by_node = binding_effects_by_node(wal)

    # Iterate the UNION of checkpoint nodes and WAL nodes. A node present in the WAL but ABSENT from the
    # binding checkpoint is the first-write-crashed case (intent-first appended the claim/first event but
    # the very first binding atomic-replace never landed). The WAL is the source of truth, so we
    # RECONSTRUCT such a node from its event chain rather than silently dropping its events.
    result: dict = {}
    for node_address in set(bindings) | set(events_by_node):
        node_events = sorted(events_by_node.get(node_address, []), key=lambda ev: ev["seq"])
        if node_address in bindings:
            # Copy so the input map is never mutated (idempotency: replay is a pure function of inputs).
            current = dict(bindings[node_address])
        elif node_events:
            # Reconstruct the pre-image base from the FIRST TRANSITION event (its from_state +
            # expected_generation are the pre-image the chain applies against); last_applied_seq=0
            # so every event is pending. JOURNAL-ONLY rows (expected_generation is None — the
            # §6.3/RR-4 escalation + visibility rows, never lifecycle transitions) cannot seed a
            # reconstruction: a row keyed to a non-binding identity (e.g. an ORPHAN escalation
            # keyed by its tmux_target) must never materialize a PHANTOM binding the next sweep
            # would then try to classify/necro.
            first = next(
                (ev for ev in node_events if ev.get("expected_generation") is not None), None
            )
            if first is None:
                continue  # journal-only chain — nothing to reconstruct
            current = {
                "node_address": node_address,
                "state": first.get("from_state"),
                "generation": first.get("expected_generation"),
                "last_applied_seq": 0,
            }
        else:
            continue  # unreachable (node came from one of the two sets), defensive
        for event in node_events:
            current = _apply_one(current, event)
        result[node_address] = current
    return result


def _apply_one(binding: dict, event: dict) -> dict:
    """Apply (or skip) ONE WAL event against ``binding`` per the §2.10 pre-image-CAS rule.

    A forward apply merges the ``binding_delta``, then sets ``state`` to the record's authoritative
    ``to_state`` and ``node_address`` to the record's ``node_address`` (authoritative over the delta
    — the mirror of ``executor.transition``'s candidate rule), and sets ``generation`` +
    ``owner_token`` to the event's POST-commit values.

    Returns the binding (possibly the same dict, possibly a forward-applied copy). Never mutates the
    input ``binding`` — a forward apply works on a copy so the caller's prior-pass result is stable
    (idempotency).
    """
    watermark = binding.get("last_applied_seq", 0)
    if event["seq"] <= watermark:
        # Below the watermark — already accounted for. Replay by generation alone (ignoring the seq
        # watermark) would re-apply an old event; the watermark guard is load-bearing.
        return binding

    # Event disposition is the single executable authority for whether replaying this row may
    # change a binding. Journal-only recovery/evidence consumers can carry generation and delta
    # fields for attribution; those fields are never canonical binding effects.
    if not wal_policy.replay_changes_binding(event):
        return binding

    live_generation = binding.get("generation")

    generation_preserving = (
        event.get("expected_generation") is not None
        and event.get("expected_generation") == event.get("generation")
    )

    # A generation-preserving own-slice commit cannot use generation to distinguish pending from
    # landed: its pre- and post-image generations are deliberately equal. The watermark already did
    # that job above. Reaching here proves the row is pending, so a matching live generation must
    # apply its delta and advance the watermark. A checkpointed row never reaches this branch.
    if generation_preserving and live_generation != event["expected_generation"]:
        return binding

    # Already-landed generation-ADVANCING transition: the binding atomic-replace committed before the
    # crash, so the binding already reflects this event's post-commit generation. NO-OP skip
    # (re-applying would double-bump). Equal-generation rows are handled by the watermark rule above.
    if not generation_preserving and live_generation == event["generation"]:
        return binding

    # The pre-image CAS: apply ONLY when the live generation matches the event's expected (pre-image)
    # generation. A mismatch here (neither pre-image NOR post-commit) is a wrong-pre-image event — it
    # must NOT be blindly applied (it would corrupt state), and the watermark is NOT advanced.
    if live_generation != event["expected_generation"]:
        return binding

    # APPLY: forward the binding to the event's post-commit image (on a copy — never mutate the input).
    applied = dict(binding)
    delta = event.get("binding_delta") or {}
    applied.update(delta)
    # Identity + lifecycle state are AUTHORITATIVE from the record, never the delta — the exact
    # mirror of executor.transition's candidate rule (candidate["node_address"]/candidate["state"]
    # = target_state, executor.py §4.2 block). The executor sets state from the legality-checked
    # target_state at commit time, so a committed transition whose caller delta omitted `state`
    # (STEP4/STEP5, collapse, watchdog_nonresponse all do) must replay to the record's to_state,
    # not the un-advanced pre-image (review executor-1 / WAL-01). Placement AFTER the delta merge
    # is load-bearing (the authoritative fields override a divergent/smuggled delta value), and the
    # generation-advancing already-landed check above firing before this apply prevents a landed
    # lifecycle transition from being re-applied. Generation-preserving rows use the watermark as
    # their landed discriminator and deliberately reach this branch while pending.
    applied["node_address"] = event["node_address"]
    applied["state"] = event["to_state"]
    applied["generation"] = event["generation"]
    applied["owner_token"] = event["owner_token"]
    if event.get("lease_epoch") is not None:
        applied["lease_epoch"] = event["lease_epoch"]
    applied["last_applied_seq"] = event["seq"]
    return applied


# ===========================================================================
# PART B — reconcile_on_restart: the §5.1 five-branch classification sweep.
# ===========================================================================

def reconcile_on_restart(executor, tmux) -> ReconcileReport:
    """Boot-once reconcile: replay the WAL, then classify every binding against live tmux (§5.1).

    Steps (§5.1 / §2.10):
      1. inside the ACQUIRED per-mutation EX lock (``executor.lock_path()``, §4.3 — one critical
         section, so ``_lock_held=True`` is true-by-fact): ``ledger.load_wal()`` (torn-tail-
         tolerant) + ``replay_wal`` -> persist the replayed map via
         ``ledger.write_binding(..., _lock_held=True)`` (one atomic-replace; the recovery
         checkpoint). The lock releases before step 3's sweep (its mutations re-take it).
      2. ``tmux.list_targets()`` -> the live targets + their pane_pid/pane_dead.
      3. per binding, the §5.1 five-branch classification:
           recorded-terminal                                 -> LEAVE (reconcile-EXACTLY-once)
           recorded-alive & tmux-present & uuid-matches       -> ADOPT (resume ownership)
           recorded-alive & tmux-absent/pane_dead, LEAF       -> NECRO (stamp DIED_INFRA, state→failed
                                                                 per §3.6, bump lease_epoch, append
                                                                 run-ledger)
           recorded-alive & tmux-absent/pane_dead, COORDINATOR-> mark dead (coordinator_died event, no
                                                                 terminal_signal stamp per §3.6),
                                                                 bump epoch, append, AND ESCALATE
      4. tmux-present & NO binding (alive-but-unowned)         -> ESCALATE orphan.
      5. resume-not-double-spawn L1: NEVER spawn here — the L1 binding is classified by the SAME five
         branches; genesis reads the verdict and decides spawn-vs-resume (§7). By having NO spawn path
         in this module the no-double-spawn invariant (F35) holds structurally.

    Every binding MUTATION routes through the REAL ``executor`` (single writer); only the §2.10-
    sanctioned replay checkpoint writes the binding map directly (inside the per-mutation EX lock
    it acquires for the checkpoint critical section).
    """
    return _reconcile(executor, tmux, replay=True)


def reconcile_tick(executor, tmux, detector) -> ReconcileReport:
    """Continuous reconcile on the watchdog timer (§5.2): the SAME §5.1 sweep, edge-triggered.

    Differences from ``reconcile_on_restart`` (§5.2):
      * NO WAL replay — replay is a boot-once recovery step (§5.1 step 1); a steady-state tick re-runs
        only the divergence classification.
      * liveness is re-derived per node from the ``detector`` (``detector.liveness(node_address)`` ->
        the canonical ``working|waiting|idle|dead`` verdict) so the sweep reflects ACTUAL tmux, not a
        stale recorded ``liveness_state``. A ``dead`` liveness verdict is the owned-but-dead trigger,
        equivalent to a tmux-absent/pane_dead pane in §5.1.
      * EDGE-TRIGGERED: only a state/condition CHANGE appends a run-ledger row. A terminal binding and
        a steady-healthy node are LEFT untouched (no WAL append), exactly as the executor's
        edge-triggered own-slice writes are (§3.5).
    """
    return _reconcile(executor, tmux, replay=False, detector=detector)


def _reconcile(executor, tmux, *, replay: bool, detector=None) -> ReconcileReport:
    """Shared sweep body for on-restart (replay=True) and tick (replay=False / detector-driven).

    The classification (the §5.1 five branches) is identical; the two callers differ only in (a)
    whether the WAL is replayed first and (b) whether liveness is read from the recorded binding (boot,
    where the actual tmux listing is the ground truth) or re-derived from the detector (tick).
    """
    # (1) Boot-only: replay the WAL onto the binding map and persist it as one recovery checkpoint.
    if replay:
        # The replay checkpoint is a REAL critical section: WAL-load + replay + whole-map write all
        # run inside the SAME held EX lock (the §4.3 per-mutation serialization domain,
        # executor.lock_path()), so ``_lock_held=True`` is true-by-fact, not by flag (review
        # reconcile-2 / SWCAS-01 — genesis's brief STEP1+2 acquire releases before this runs, so
        # nothing else held the lock here). Reading load_wal/all_nodes INSIDE the lock makes the
        # read -> replay -> write atomic per §4.3. The lock RELEASES before the sweep below — the
        # executor re-takes this SAME lock per mutation and fcntl flock is not re-entrant, so
        # widening this scope over the classification sweep would self-deadlock.
        with store.file_lock(_executor_mod.lock_path(), shared=False):
            wal = ledger.load_wal()
            if wal:
                replayed = replay_wal(ledger.all_nodes(), wal)
                ledger.write_binding(replayed, _lock_held=True)

    # (2) The live tmux listing (the ONLY actual-state source the sweep trusts — §5.4: evidence-based).
    targets = tmux.list_targets()

    # The current (post-replay) binding map, and the set of tmux_targets each binding owns.
    bindings = ledger.all_nodes()
    owned_targets = {b.get("tmux_target") for b in bindings.values() if b.get("tmux_target")}

    adopted: list = []
    necroed: list = []
    escalations: list = []

    # (3) Classify every binding (the §5.1 five branches). L1 is classified by the SAME branches —
    # no spawn happens here (resume-not-double-spawn, §5.1 step 5).
    for node_address, binding in bindings.items():
        state = binding.get("state")

        # Branch 4 — recorded-TERMINAL: LEAVE (reconcile-EXACTLY-once). No action, no WAL append; a
        # re-run is a no-op (the binding is byte-for-byte unchanged across passes).
        if states.is_terminal(state):
            continue

        # PRE-SPAWN ``planned`` slots own NO pane (INT-4(a)): their tmux_target is still the
        # registration PLACEHOLDER (the bare session name — real ``list_targets`` keys are ALWAYS
        # '<session>:<window>.<pane>' triples, so the placeholder can never match), and no actor
        # has opened. Classifying them owned-but-dead necro'd EVERY planned slot on boot
        # (planned->failed is legal via the F5 fold), which made genesis's F21 claim-the-survivor
        # branch unreachable and forced the epoch-resetting re-register leg instead. A planned
        # slot is pure intent: nothing live to adopt, nothing dead to reap — the claim path
        # (genesis STEP 5 / register_and_spawn_child) is its owner. SKIP.
        if state == "planned":
            continue

        target_name = binding.get("tmux_target")
        target = targets.get(target_name)

        # "tmux-present" means the pane is listed AND alive (pane_dead == 0). A present-but-pane_dead
        # pane is owned-but-dead — EQUIVALENT to a gone pane for the necro decision (§5.1).
        present_alive = target is not None and not _pane_dead(target)

        # Tick path: re-derive liveness from the detector (ACTUAL tmux). A `dead` verdict is the
        # owned-but-dead trigger even if the (cached) tmux listing still lists the pane.
        if detector is not None and present_alive:
            verdict = detector.liveness(node_address)
            if getattr(verdict, "state", None) == "dead":
                present_alive = False

        if present_alive:
            # Branch 1 — ADOPT, but ONLY when the live session_uuid matches the recorded one. A
            # present pane whose uuid differs is a DIFFERENT incarnation; adopting it would resume
            # ownership of the wrong process (the uuid match is load-bearing).
            if _uuid_matches(binding, target):
                adopted.append(node_address)
            # uuid-MISMATCH: NOT an adopt (a different incarnation). It is also not owned-but-dead
            # (a pane IS present). Left for cluster-② / a later edge — not silently adopted, not
            # reaped. (The frozen test asserts only "not adopted".)
            continue

        # Owned-but-dead (tmux-absent OR pane_dead). Coordinator vs leaf asymmetry (§5.4).
        if _is_coordinator(node_address, binding, bindings):
            # Branch 3 — COORDINATOR-DIED: mark dead (§3.6 row), stamp coordinator_died EVENT, bump
            # epoch, append, AND ESCALATE (recover-vs-reap is cluster-② — NOT decided here, §5.4).
            # The escalation fires EITHER WAY (cluster-② must see the death), but the routed result
            # keeps it honest: an aborted stamp is never reported as committed (necro_ok carries it).
            result = _terminal_necro(
                executor,
                node_address,
                binding,
                terminal_signal=_COORDINATOR_DIED_SIGNAL,
                target_state=_COORDINATOR_DIED_STATE,
                event=_COORDINATOR_DIED_EVENT,
                summary="coordinator_died: owned-but-dead coordinator pane (§5.4) — escalating",
            )
            escalations.append(
                {
                    "node_address": node_address,
                    "kind": "coordinator_died",
                    "reason": "owned-but-dead coordinator: recover-vs-reap is cluster-② (§5.4)",
                    "necro_ok": result.ok,
                    "errors": list(result.errors),
                }
            )
        else:
            # Branch 2 — LEAF-NECRO: stamp DIED_INFRA, state→failed (§3.6), bump lease_epoch, append
            # run-ledger. ROUTE the result: a CAS-aborted necro surfaces as an escalation, never in
            # `necroed` as if it committed (the no-result-swallowing convention).
            result = _terminal_necro(
                executor,
                node_address,
                binding,
                terminal_signal=_DIED_INFRA_SIGNAL,
                target_state=_DIED_INFRA_STATE,
                event=_DIED_INFRA_EVENT,
                summary="leaf-necro: owned-but-dead leaf pane (§5.1) — reaped DIED_INFRA (state→failed, §3.6)",
            )
            if result.ok:
                necroed.append(node_address)
            else:
                escalations.append(
                    {
                        "node_address": node_address,
                        "kind": "necro_failed",
                        "reason": "leaf-necro terminal write aborted",
                        "errors": list(result.errors),
                    }
                )

    # (4) Orphans — tmux-present with NO binding (alive-but-unowned). ESCALATE (§5.1 branch 5). An
    # orphan has no node_address; the tmux_target IS its only identity.
    for target_name, target in targets.items():
        if target_name in owned_targets:
            continue
        if _pane_dead(target):
            # A dead-but-unowned pane is not a live orphan to escalate (nothing alive is unaccounted
            # for). Only an ALIVE-but-unowned target is the §5.1 orphan.
            continue
        escalations.append(
            {
                "node_address": target_name,
                "tmux_target": target_name,
                "kind": "orphan",
                "reason": "alive-but-unowned tmux target (no binding): orphan -> cluster-② / L1 (§5.1)",
            }
        )

    return ReconcileReport(adopted=adopted, necroed=necroed, escalations=escalations)


# ---------------------------------------------------------------------------
# The single terminal-necro write — routes through the REAL executor (§4.3 single writer).
# ---------------------------------------------------------------------------

def _terminal_necro(executor, node_address: str, binding: dict, *, terminal_signal: str,
                    target_state: str, event: str, summary: str):
    """Drive an owned-but-dead binding to its §3.6 death-class state via the executor (the ONE writer;
    never raw): a leaf DIED_INFRA lands ``failed``; a coordinator_died lands ``dead``. RETURNS the
    ``TransitionResult`` — callers route it (no result-swallowing; an aborted write must never read as
    a committed necro).

    The terminal write (§5.4 "single-writer terminal write"): drive to ``target_state`` (sourced from
    ``states.TERMINAL_VOCAB`` by the caller), stamp the ``terminal_signal`` death class +
    ``terminal_signal_at``, BUMP ``lease_epoch`` (fence the prior incarnation — a stale actor returning
    after this necro carries a lower-epoch token and loses), and append the run-ledger death event. The
    ``liveness_state`` stays ``dead`` for BOTH legs — the liveness layer records the factual process
    death (a distinct axis from the lifecycle state, §3.2). All of it rides ONE ``executor.transition``
    (CAS-guarded, intent-first, crash-safe via ``last_applied_seq``): the candidate's generation bump,
    the delta, the epoch/token rotation, and the WAL append are one atomic commit.

    The CAS preconditions are sourced from the LIVE binding (``expected_state`` = its current state,
    ``expected_generation`` = its current generation), and ``expected_owner_token=None`` — the daemon's
    reconcile is the un-fenced internal mutator (it is not a returning actor presenting a token; the EX
    lock is the serialization, §4.3). The bumped epoch + re-minted token are rotated into the SAME
    candidate as the state change (F-012, no split window).
    """
    new_lease_epoch = fencing.advance_epoch(binding)
    new_owner_token = fencing.mint_owner_token(
        node_address,
        binding.get("subagent_id"),
        binding.get("session_uuid"),
        new_lease_epoch,
    )
    from .spawn import chokepoint

    death_delta = chokepoint.actor_death_accounting_delta(
        binding,
        event=event,
        reason=summary,
    )
    binding_delta = {
        "state": target_state,
        "liveness_state": "dead",  # the liveness axis is factually dead regardless of the §3.6 state
        "lease_epoch": new_lease_epoch,
        "owner_token": new_owner_token,
    }
    binding_delta.update(death_delta)
    # Stamp terminal_signal ONLY when this death class HAS one (§3.6): leaf-necro carries DIED_INFRA;
    # coordinator_died carries NONE (its class lives in the run-ledger event + the escalation kind, not
    # in a binding terminal_signal — preserving the §3.6 spelling-split). A None stamp is omitted, not
    # written as a literal None terminal_signal value.
    if terminal_signal is not None:
        binding_delta["terminal_signal"] = terminal_signal
        binding_delta["terminal_signal_at"] = _now()
    result = executor.transition(
        node_address,
        expected_state=binding["state"],
        expected_generation=binding["generation"],
        expected_owner_token=None,  # daemon-internal necro — the EX lock serializes, no actor token
        target_state=target_state,
        binding_delta=binding_delta,
        new_lease_epoch=new_lease_epoch,
        new_owner_token=new_owner_token,
        event=event,
        summary=summary,
    )
    if (
        result is not None
        and getattr(result, "ok", False)
        and death_delta.get("respawn_parked_at")
        and not binding.get("respawn_parked_at")
    ):
        chokepoint.journal_respawn_parked(
            node_address,
            getattr(result, "binding", None) or binding,
        )
    return result


# ---------------------------------------------------------------------------
# Classification helpers.
# ---------------------------------------------------------------------------

def _pane_dead(target: dict) -> bool:
    """True iff the tmux target reports its pane dead (``pane_dead`` truthy; §2.11 shape).

    tmux reports ``pane_dead`` as ``0``/``1`` (or the string ``"0"``/``"1"``). A present-but-pane_dead
    pane is owned-but-dead — EQUIVALENT to a gone pane for the necro decision (§5.1). Treating
    ``pane_dead == 1`` as 'alive' (checking key presence alone) would wrongly adopt a crashed pane.
    """
    value = target.get("pane_dead", 0)
    if isinstance(value, str):
        return value.strip() not in ("", "0")
    return bool(value)


def _uuid_matches(binding: dict, target: dict) -> bool:
    """True iff the live pane's ``session_uuid`` matches the binding's recorded one (§5.1 branch 1).

    A present pane whose uuid DIFFERS is a different incarnation; adopting it would resume ownership of
    the wrong process. When the tmux listing does NOT carry a ``session_uuid`` (the minimal §2.11 shape
    is ``{pane_pid, pane_dead, window_activity}`` — uuid is an OPTIONAL enrichment), there is no
    contradicting evidence, so a present alive pane is taken as the recorded incarnation (adopt). Only
    an EXPLICIT uuid mismatch refuses the adopt.
    """
    live_uuid = target.get("session_uuid")
    if live_uuid is None:
        return True  # no uuid evidence in the minimal listing -> not a contradiction -> adopt
    return live_uuid == binding.get("session_uuid")


def _is_coordinator(node_address: str, binding: dict, bindings: dict) -> bool:
    """True iff ``node_address`` is a COORDINATOR — it has at least one descendant binding (§5.4).

    Primary discriminator: another binding in the map names this node as its ``parent_address`` (the
    §3.1 denormalized reconcile-speed pointer). Fallback (when no binding carries ``parent_address``):
    address-prefix arithmetic (§3.1 "topology is derivable by prefix arithmetic") — a child address is
    ``<this-node's-path>/<segment>#<seat>``, so a node is a coordinator iff some OTHER binding's path
    is a strict descendant of this node's path.
    """
    for other_address, other in bindings.items():
        if other_address == node_address:
            continue
        if other.get("parent_address") == node_address:
            return True
    # Fallback: prefix arithmetic on the one-spine address (path#seat). Strip the seat suffix, then a
    # descendant's path begins with "<this-path>/".
    this_path = node_address.split("#", 1)[0]
    for other_address in bindings:
        if other_address == node_address:
            continue
        other_path = other_address.split("#", 1)[0]
        if other_path.startswith(this_path + "/"):
            return True
    return False


def _now() -> str:
    """The single canonical UTC clock (DAEMON §4.6) for the ``terminal_signal_at`` stamp."""
    from . import clock

    return clock.now_utc()
