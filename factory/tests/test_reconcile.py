"""Increment 7 — reconcile (WAL replay + on-restart sweep). FROZEN ACCEPTANCE (RED-first).

The crash-recovery half of the daemon: intent-first commit (DAEMON §4.4) leaves the WAL AHEAD of the
binding (the intent/event row is appended+fsync'd BEFORE the binding atomic-replace), so on restart we
must (1) deterministically REPLAY any committed-but-not-yet-checkpointed WAL event, then (2) classify
every binding against live tmux (the §5.1 five branches).

AUTHORITATIVE INTERFACE — IMPLEMENTATION-PLAN §2.10 (transcribed):

    replay_wal(bindings: dict[str,dict], wal: list[dict]) -> dict[str,dict]
        # for each event with seq > binding.last_applied_seq[node]:
        #   if binding.generation == event.expected_generation:   # the CAS PRE-IMAGE matches -> apply:
        #       apply binding_delta; set generation+owner_token to post-commit; stamp last_applied_seq=seq
        #   elif binding.generation == event.generation:           # already landed -> NO-OP skip (idempotent)
        # BATCH all pending events for a node into ONE atomic-replace (FORK-REPLAY = one recovery
        # checkpoint per node, not one replace per event). Deterministic + idempotent.

    reconcile_on_restart(executor, tmux) -> ReconcileReport     # boot-once: replay then sweep (§5.1)
    reconcile_tick(executor, tmux, detector) -> ReconcileReport # continuous, edge-triggered (§5.2)

BIAS TO REAL (Lesson 7, register): REAL WAL + binding files on disk under a tmp RUNTIME_ROOT (real
ledger.build_wal_record / append_wal / write_binding / load_wal), the REAL executor module (the single
writer — every reconcile mutation routes through it, never a raw binding poke), and the REAL detector for
the tick. The ONLY fake is tmux.list_targets() -> {tmux_target: {pane_pid, pane_dead, window_activity}}
(the FROZEN §2.11 shape) — justified because the concrete tmux.py is not built until Increment 9; its
shape is validated against real tmux by the Inc-9 contract test (Lesson 6). The fake is MINIMAL: only
list_targets().

LOAD-BEARING (each test kills a named mutant):
  * replay re-applies a PENDING event (mutant: skip pending -> WAL-ahead-of-binding never recovered)
  * replay SKIPS an already-landed event (mutant: re-apply landed -> double-applied / generation wrong)
  * the pre-image CAS guards replay (mutant: apply without checking expected_generation -> a mismatched
    event corrupts state)
  * replay is IDEMPOTENT (replaying the same WAL twice == once)
  * §5.1 ADOPT (recorded-alive, tmux-present, session_uuid matches)
  * §5.1 LEAF-NECRO (recorded-alive, tmux-gone, leaf) stamps died_* + bumps lease_epoch + appends
  * §5.1 COORDINATOR-DIED ESCALATES (mutant: necro a coord silently without escalate)
  * §5.1 recorded-TERMINAL is LEFT EXACTLY ONCE (mutant: re-act on a terminal binding -> 2nd necro/append)
  * §5.1 ORPHAN (tmux-present, NO binding) ESCALATES (mutant: ignore orphan)

This file is TESTS ONLY. harnessd/reconcile.py does NOT exist yet — import fails RED, by design.
"""

import copy

import pytest

import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger

# The keystone under construction — RED until harnessd/reconcile.py exists. A bare module import keeps
# the failure a clean ImportError (collection fails loud) rather than a per-test AttributeError.
import harnessd.reconcile as reconcile  # noqa: E402  (intentional: RED-first on the not-yet-built module)


# ---------------------------------------------------------------------------
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so every pathless real
# ledger/executor call (read_binding / append_wal / write_binding / the EX lock)
# targets the test tree. Restore the prior value so tests don't leak runtime state.
# (Mirrors test_executor.py::executor_runtime exactly — established suite pattern.)
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


# ---------------------------------------------------------------------------
# Minimal fake tmux — the FROZEN §2.11 surface, list_targets() ONLY. Nothing else.
# Returns {tmux_target: {pane_pid, pane_dead, window_activity}}.
# ---------------------------------------------------------------------------

class FakeTmux:
    """The ONLY faked runtime (Lesson 6: a runtime-not-yet-built mock for Increment-9's tmux.py).

    Exposes EXACTLY list_targets() and nothing more — keeping it minimal means the Inc-9 contract
    test (real tmux vs this shape) is the single place the shape is pinned. ``targets`` maps a
    tmux_target string to its {pane_pid, pane_dead, window_activity} dict.
    """

    def __init__(self, targets):
        self._targets = targets

    def list_targets(self):
        return dict(self._targets)


# ---------------------------------------------------------------------------
# Real-binding seeding helpers — write REAL bindings to the REAL on-disk binding
# ledger via ledger.write_binding(_lock_held=True) (the §2.6 direct-seed path the
# executor tests use). No fakes: the post-reconcile assertions read these back
# through the real ledger.all_nodes()/read_binding.
# ---------------------------------------------------------------------------

def _binding(
    node_address,
    *,
    level,
    state="running",
    generation=4,
    lease_epoch=2,
    last_applied_seq=0,
    session_uuid="sess-aaaa-0001",
    parent_address=None,
    tmux_target=None,
    terminal_signal=None,
    liveness_state="working",
    extra=None,
):
    """One full-shape binding (DAEMON §3 schema, CAS-bearing field set the executor reads)."""
    subagent = "subagent-" + node_address.replace("/", "-").replace("#", "-")
    owner_token = fencing.mint_owner_token(node_address, subagent, session_uuid, lease_epoch)
    b = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": subagent,
        "session_uuid": session_uuid,
        "tmux_target": tmux_target if tmux_target is not None else "harness:" + node_address,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": last_applied_seq,
        "liveness_state": liveness_state,
        "terminal_signal": terminal_signal,
        "terminal_signal_at": None,
    }
    if extra:
        b.update(extra)
    return b


def _seed(*bindings):
    """Atomic-replace the whole REAL binding map with the given bindings (lock-held seed path)."""
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _target_alive(pid=4321):
    return {"pane_pid": pid, "pane_dead": 0, "window_activity": "0"}


def _target_dead(pid=4321):
    return {"pane_pid": pid, "pane_dead": 1, "window_activity": "0"}


# ---------------------------------------------------------------------------
# ReconcileReport access helpers — read the three §2.13 fields off the report
# regardless of whether it's the frozen dataclass or a structurally-equal object.
# ---------------------------------------------------------------------------

def _adopted(report):
    return list(getattr(report, "adopted"))


def _necroed(report):
    return list(getattr(report, "necroed"))


def _escalations(report):
    return list(getattr(report, "escalations"))


def _escalated_nodes(report):
    """The node_address of every escalation entry (escalations is a list of dicts, §2.13)."""
    out = []
    for esc in _escalations(report):
        if isinstance(esc, dict):
            out.append(esc.get("node_address") or esc.get("node") or esc.get("address"))
        else:
            out.append(esc)
    return out


# ===========================================================================
# PART A — replay_wal: deterministic + idempotent re-apply with the pre-image CAS.
# Pure-function level (REAL build_wal_record records, REAL binding dicts in-memory).
# ===========================================================================

NODE = "proj/widget#exec"


def _wal_record(*, seq, expected_generation, generation, binding_delta, owner_token,
                node_address=NODE, event="transition", from_state="spawning",
                to_state="running", lease_epoch=3):
    """One REAL WAL record via the real builder (carries crc32 + the transition pre/post image)."""
    return ledger.build_wal_record(
        node_address=node_address,
        event=event,
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_generation,
        generation=generation,
        lease_epoch=lease_epoch,
        owner_token=owner_token,
        binding_delta=binding_delta,
        summary="replay-test",
        artifacts=[],
        seq=seq,
    )


def test_replay_reapplies_a_pending_event(runtime):
    """A PENDING event (seq > last_applied_seq, generation == event.expected_generation) is RE-APPLIED.

    Intent-first (§4.4) appended this WAL row + fsync'd it BEFORE the binding atomic-replace, then
    crashed -> the binding is one generation BEHIND the WAL. Replay must re-apply it.
    Mutant killed: skip pending events -> the WAL-ahead-of-binding state is NEVER recovered.
    """
    # binding is at generation 4 / seq 0; the WAL holds the next committed transition (4 -> 5).
    binding = _binding(NODE, level="L5", state="spawning", generation=4, last_applied_seq=0)
    post_token = "proj/widget#exec:sub:sess-aaaa-0001:3"
    pending = _wal_record(
        seq=5,
        expected_generation=4,        # pre-image MATCHES the live binding.generation
        generation=5,                 # post-commit generation
        owner_token=post_token,
        binding_delta={"state": "running", "session_uuid": "sess-bbbb-0002"},
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])

    rb = out[NODE]
    assert rb["generation"] == 5, "replay must advance generation to the event's POST-commit value"
    assert rb["owner_token"] == post_token, "replay must set owner_token to the post-commit token"
    assert rb["last_applied_seq"] == 5, "replay must stamp last_applied_seq = seq (the watermark)"
    assert rb["state"] == "running", "replay must apply the binding_delta"
    assert rb["session_uuid"] == "sess-bbbb-0002", "replay must apply EVERY field in binding_delta"


def test_replay_reapplies_generic_generation_advancing_transition_by_shape(runtime):
    """The repair transition surface permits a caller label; its CAS shape remains canonical."""
    binding = _binding(NODE, level="L5", state="running", generation=4, last_applied_seq=0)
    pending = _wal_record(
        seq=5,
        expected_generation=4,
        generation=5,
        owner_token=binding["owner_token"],
        event="operator_repair_transition",
        binding_delta={"state": "blocked"},
        from_state="running",
        to_state="blocked",
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])

    assert out[NODE]["state"] == "blocked"
    assert out[NODE]["generation"] == 5
    assert out[NODE]["last_applied_seq"] == 5


def test_replay_reapplies_a_pending_generation_preserving_own_slice_event(runtime):
    """A WAL-ahead own-slice row is pending by watermark even though generation is unchanged."""
    binding = _binding(
        NODE,
        level="L5",
        state="running",
        generation=4,
        last_applied_seq=11,
        extra={"wake_attempt_count": 1},
    )
    pending = _wal_record(
        seq=12,
        expected_generation=4,
        generation=4,
        owner_token=binding["owner_token"],
        event="wake_verify_pending",
        from_state="running",
        to_state="running",
        binding_delta={
            "wake_attempt_count": 2,
            "wake_pending_ack_offset": 88,
        },
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])

    assert out[NODE]["generation"] == 4
    assert out[NODE]["wake_attempt_count"] == 2
    assert out[NODE]["wake_pending_ack_offset"] == 88
    assert out[NODE]["last_applied_seq"] == 12


def test_replay_never_reapplies_a_checkpointed_generation_preserving_event(runtime):
    """The watermark, not equal generation, proves an own-slice row already landed."""
    binding = _binding(
        NODE,
        level="L5",
        state="running",
        generation=4,
        last_applied_seq=13,
        extra={
            "wake_attempt_count": 0,
            "wake_pending_ack_offset": None,
        },
    )
    checkpointed = _wal_record(
        seq=12,
        expected_generation=4,
        generation=4,
        owner_token=binding["owner_token"],
        event="wake_verify_pending",
        from_state="running",
        to_state="running",
        binding_delta={
            "wake_attempt_count": 2,
            "wake_pending_ack_offset": 88,
        },
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [checkpointed])

    assert out[NODE] == binding


def test_replay_never_applies_a_replay_neutral_equal_generation_audit_row(runtime):
    """Equal generations cannot turn a journal-only evidence row into binding state."""
    binding = _binding(
        NODE,
        level="L5",
        state="running",
        generation=4,
        last_applied_seq=11,
    )
    audit = _wal_record(
        seq=12,
        expected_generation=4,
        generation=4,
        owner_token=binding["owner_token"],
        event="stale_return_ignored",
        from_state="running",
        to_state="running",
        binding_delta={
            "presented_owner_token": "stale-token",
            "attempted": "heartbeat",
        },
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [audit])

    assert out[NODE] == binding
    assert "presented_owner_token" not in out[NODE]
    assert "attempted" not in out[NODE]


def test_replay_skips_an_already_landed_event(runtime):
    """An ALREADY-LANDED event (binding.generation == event.generation) is a NO-OP skip.

    The binding atomic-replace DID land before the crash, so the binding already reflects the event.
    Mutant killed: re-apply a landed event -> generation double-bumped / binding_delta re-applied ->
    state corrupted.
    """
    # binding already AT generation 5 (the event's post-commit generation) and watermark already 5.
    landed_token = "proj/widget#exec:sub:sess-landed:9"
    binding = _binding(
        NODE, level="L5", state="running", generation=5, last_applied_seq=5,
        extra={"owner_token": landed_token},
    )
    before = copy.deepcopy(binding)
    # The same event whose post-commit generation == the live generation (already landed).
    already = _wal_record(
        seq=5,
        expected_generation=4,
        generation=5,                 # == binding.generation -> already-landed branch
        owner_token=landed_token,
        binding_delta={"state": "running"},
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [already])

    assert out[NODE]["generation"] == 5, "an already-landed event must NOT re-bump generation"
    assert out[NODE]["owner_token"] == before["owner_token"], "landed event must not re-rotate token"
    assert out[NODE]["last_applied_seq"] == 5, "watermark unchanged on a landed-event skip"


def test_replay_below_watermark_is_skipped(runtime):
    """An event at seq <= last_applied_seq is below the watermark -> never replayed.

    Mutant killed: replay by generation alone (ignoring the seq watermark) -> an old event re-applies.
    """
    binding = _binding(NODE, level="L5", state="running", generation=7, last_applied_seq=10)
    before = copy.deepcopy(binding)
    stale = _wal_record(
        seq=6,                        # 6 <= 10 -> below the watermark
        expected_generation=6,
        generation=7,
        owner_token="x:y:z:1",
        binding_delta={"state": "blocked"},
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [stale])

    assert out[NODE] == before, "an event at seq <= last_applied_seq must be skipped entirely"


def test_replay_preimage_cas_guard_blocks_mismatched_event(runtime):
    """The pre-image CAS guards replay: an event whose expected_generation != live generation AND whose
    post-commit generation != live generation is NEITHER apply NOR already-landed -> it must NOT be
    blindly applied (it would corrupt state).

    Mutant killed: apply binding_delta without checking expected_generation -> a mismatched event
    overwrites the binding with a wrong-pre-image transition.
    """
    # live generation is 4; the event expects pre-image 99 (matches neither expected NOR post 100).
    binding = _binding(NODE, level="L5", state="running", generation=4, last_applied_seq=0)
    before = copy.deepcopy(binding)
    mismatched = _wal_record(
        seq=5,
        expected_generation=99,       # pre-image does NOT match live generation 4
        generation=100,               # post-commit does NOT match either -> not "already landed"
        owner_token="bogus:token:0:0",
        binding_delta={"state": "dead", "terminal_signal": "FENCED"},
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [mismatched])

    assert out[NODE]["generation"] == before["generation"], (
        "a pre-image-mismatched event must NOT be applied (no generation change)"
    )
    assert out[NODE]["state"] == before["state"], "mismatched binding_delta must NOT be applied"
    assert "terminal_signal" not in out[NODE] or out[NODE]["terminal_signal"] is None, (
        "a mismatched event must not smuggle a terminal_signal into the binding"
    )


def test_replay_is_idempotent_twice_equals_once(runtime):
    """Replaying the SAME WAL twice yields the SAME bindings (idempotency, FORK-REPLAY).

    Mutant killed: a replay that re-applies on the second pass (e.g. re-bumps generation because it
    skips the watermark stamp) diverges between pass-1 and pass-2.
    """
    binding = _binding(NODE, level="L5", state="spawning", generation=4, last_applied_seq=0)
    pending = _wal_record(
        seq=5, expected_generation=4, generation=5,
        owner_token="proj/widget#exec:sub:s:5",
        binding_delta={"state": "running"},
    )

    once = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])
    twice = reconcile.replay_wal(copy.deepcopy(once), [pending])

    assert twice == once, "replay must be idempotent: replaying the same WAL twice == once"


def test_shared_binding_effect_projection_preserves_related_replay(runtime):
    """Recovery and committed validation consume the same primary+related WAL effects."""
    parent = "proj#exec"
    child = "proj/child#exec"
    parent_token = "proj#exec:sub:session:2"
    child_token = "proj/child#exec:sub:session:2"
    parent_binding = _binding(
        parent,
        level="L3",
        state="running",
        generation=3,
        last_applied_seq=0,
        extra={"owner_token": parent_token, "lease_epoch": 2},
    )
    child_binding = _binding(
        child,
        level="L4",
        state="claimed",
        generation=1,
        last_applied_seq=0,
        extra={"owner_token": child_token, "lease_epoch": 2},
    )
    entry = ledger.build_wal_record(
        node_address=child,
        event="spawn_contracts_receipted",
        from_state="claimed",
        to_state="claimed",
        expected_generation=1,
        generation=2,
        lease_epoch=2,
        owner_token=child_token,
        binding_delta={"contract_receipts": {"brief.md": {"fingerprint": "child"}}},
        summary="shared-effect-projection",
        artifacts=[],
        seq=10,
        related_binding_deltas={
            parent: {
                "from_state": "running",
                "to_state": "running",
                "expected_generation": 3,
                "generation": 4,
                "lease_epoch": 2,
                "owner_token": parent_token,
                "binding_delta": {
                    "contract_versions": {"brief.md": {"fingerprint": "parent"}}
                },
            }
        },
    )

    effects = reconcile.binding_effects_by_node([entry])
    replayed = reconcile.replay_wal(
        {parent: parent_binding, child: child_binding},
        [entry],
    )

    assert [effect["node_address"] for effect in effects[child]] == [child]
    assert [effect["node_address"] for effect in effects[parent]] == [parent]
    assert effects[parent][0]["seq"] == entry["seq"]
    assert effects[parent][0]["actor"] == entry["actor"]
    assert replayed == {
        parent: {
            **parent_binding,
            "contract_versions": {"brief.md": {"fingerprint": "parent"}},
            "state": "running",
            "generation": 4,
            "owner_token": parent_token,
            "lease_epoch": 2,
            "last_applied_seq": 10,
        },
        child: {
            **child_binding,
            "contract_receipts": {"brief.md": {"fingerprint": "child"}},
            "state": "claimed",
            "generation": 2,
            "owner_token": child_token,
            "lease_epoch": 2,
            "last_applied_seq": 10,
        },
    }


def test_replay_batches_multiple_pending_into_one_node_chain(runtime):
    """FORK-REPLAY: BATCH all pending events for a node and apply them in seq order into one result.

    Two consecutive pending events (4->5 then 5->6) must BOTH land, leaving generation 6 and the
    watermark at the last seq. Mutant killed: applying only the first / only the last pending event.
    """
    binding = _binding(NODE, level="L5", state="spawning", generation=4, last_applied_seq=0)
    ev1 = _wal_record(
        seq=5, expected_generation=4, generation=5,
        owner_token="t:5", binding_delta={"state": "running"},
        from_state="spawning", to_state="running",
    )
    ev2 = _wal_record(
        seq=6, expected_generation=5, generation=6,
        owner_token="t:6", binding_delta={"state": "blocked"},
        from_state="running", to_state="blocked",
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [ev1, ev2])

    assert out[NODE]["generation"] == 6, "both batched pending events must apply (chain 4->5->6)"
    assert out[NODE]["last_applied_seq"] == 6, "watermark must advance to the LAST applied seq"
    assert out[NODE]["state"] == "blocked", "the final binding_delta in the chain must win"
    assert out[NODE]["owner_token"] == "t:6", "owner_token must end at the last event's post-commit"


# ===========================================================================
# PART B — reconcile_on_restart: the §5.1 five-branch classification sweep.
# REAL bindings on disk, REAL executor (single-writer mutations), fake tmux only.
# ===========================================================================

ADOPT_NODE = "proj/alpha#exec"
LEAF_NODE = "proj/beta/leaf#exec"
COORD_NODE = "proj/gamma#exec"
COORD_CHILD = "proj/gamma/child#exec"
TERM_NODE = "proj/delta#exec"
ORPHAN_TARGET = "harness:proj/orphan#exec"


def test_restart_adopt_when_tmux_present_and_uuid_matches(runtime):
    """§5.1 branch 1 — recorded-alive & tmux-present & session_uuid matches -> ADOPT (resume ownership).

    Mutant killed: necro/escalate a node whose pane is alive and whose uuid matches -> a live node
    is wrongly reaped.
    """
    b = _binding(
        ADOPT_NODE, level="L5", state="running", generation=4,
        session_uuid="sess-live-001", tmux_target="harness:" + ADOPT_NODE,
    )
    _seed(b)
    tmux = FakeTmux({"harness:" + ADOPT_NODE: _target_alive()})

    report = reconcile.reconcile_on_restart(executor, tmux)

    assert ADOPT_NODE in _adopted(report), "a live, uuid-matched binding must be ADOPTED"
    assert ADOPT_NODE not in _necroed(report), "an adopted node must not be necro'd"
    assert ADOPT_NODE not in _escalated_nodes(report), "an adopted node must not be escalated"
    # The binding is NOT collapsed to a terminal state by adoption.
    assert ledger.read_binding(ADOPT_NODE)["state"] == "running", "adopt must not terminalize a live node"


def test_restart_leaf_necro_stamps_died_and_bumps_epoch_and_appends(runtime):
    """§5.1 branch 2 — recorded-alive & tmux-absent, LEAF -> NECRO: stamp DIED_INFRA, state→failed
    (§3.6), bump lease_epoch, append a run-ledger event.

    Mutant killed: a dead leaf left running (no necro) -> an owned-but-dead session is never reaped.
    Bias to real: the necro routes through the REAL executor, so we assert on the REAL on-disk binding
    (state == failed, a died_* terminal signal, lease_epoch bumped) and the REAL WAL (a death event row).
    """
    b = _binding(
        LEAF_NODE, level="L5", state="running", generation=4, lease_epoch=2,
        tmux_target="harness:" + LEAF_NODE,
    )
    _seed(b)
    wal_before = len(ledger.load_wal())
    # tmux has NO target for this leaf -> tmux-absent.
    tmux = FakeTmux({})

    report = reconcile.reconcile_on_restart(executor, tmux)

    assert LEAF_NODE in _necroed(report), "a dead leaf must be reported as necro'd"
    rb = ledger.read_binding(LEAF_NODE)
    assert rb["state"] == "failed", "§3.6 maps DIED_INFRA -> state failed (leaf-necro death class)"
    assert rb.get("terminal_signal") in {"DIED_INFRA", "DIED_INFRASTRUCTURE", "DIED_METHODOLOGY"}, (
        "leaf-necro must stamp a died_* terminal_signal (§3.6 death class)"
    )
    assert rb["lease_epoch"] > b["lease_epoch"], "leaf-necro must BUMP lease_epoch (fence the prior incarnation)"
    assert len(ledger.load_wal()) > wal_before, "leaf-necro must append a run-ledger (WAL) death event"


def test_restart_third_actor_death_parks_with_all_three_causes(runtime):
    """The process-death writer consumes the same address-local three-tries account."""
    b = _binding(
        LEAF_NODE,
        level="L5",
        state="running",
        generation=4,
        lease_epoch=2,
        tmux_target="harness:" + LEAF_NODE,
    )
    b.update(
        {
            "consecutive_failed_incarnations": 2,
            "failed_incarnation_causes": [
                {"event": "watchdog_nonresponse", "reason": "one"},
                {"event": "watchdog_runtime_failure", "reason": "two"},
            ],
        }
    )
    _seed(b)

    report = reconcile.reconcile_on_restart(executor, FakeTmux({}))

    assert LEAF_NODE in _necroed(report)
    after = ledger.read_binding(LEAF_NODE)
    assert after["consecutive_failed_incarnations"] == 3
    assert after["respawn_parked_at"]
    assert [cause["event"] for cause in after["failed_incarnation_causes"]] == [
        "watchdog_nonresponse",
        "watchdog_runtime_failure",
        "died_infrastructure",
    ]
    parked = [
        row
        for row in ledger.load_wal()
        if row.get("event") == "seat_respawn_parked"
        and row.get("node_address") == LEAF_NODE
    ]
    assert len(parked) == 1
    assert len(parked[0]["binding_delta"]["causes"]) == 3


def test_restart_coordinator_died_escalates(runtime):
    """§5.1 branch 3 — recorded-alive & tmux-absent, COORDINATOR -> mark dead, stamp coordinator_died,
    bump epoch, append, AND ESCALATE (recover-vs-reap is cluster-2, NOT decided here).

    A coordinator is a node with at least one descendant binding in the map (COORD_CHILD's
    parent_address points at it). Mutant killed: necro a dead coordinator SILENTLY (like a leaf)
    WITHOUT escalating -> the escalations list omits it.
    """
    coord = _binding(COORD_NODE, level="L2", state="running", generation=4, lease_epoch=2,
                     tmux_target="harness:" + COORD_NODE)
    child = _binding(COORD_CHILD, level="L3", state="running", generation=2, lease_epoch=1,
                     parent_address=COORD_NODE, tmux_target="harness:" + COORD_CHILD)
    _seed(coord, child)
    # tmux has the child alive but the COORDINATOR's pane is GONE.
    tmux = FakeTmux({"harness:" + COORD_CHILD: _target_alive()})

    report = reconcile.reconcile_on_restart(executor, tmux)

    assert COORD_NODE in _escalated_nodes(report), (
        "a dead COORDINATOR must ESCALATE (recover-vs-reap is cluster-2 — not silently reaped)"
    )
    rb = ledger.read_binding(COORD_NODE)
    assert rb["state"] == "dead", "coordinator-died must mark the binding dead"
    # Per §3.6 TERMINAL_VOCAB: coordinator_died carries the run-ledger EVENT + the escalation kind, and
    # terminal_signal is None (NOT the event name — "coordinator_died" is not a valid terminal_signal
    # value, and stamping it would violate the §3.6 spelling-split).
    assert rb.get("terminal_signal") is None, (
        "coordinator-died must NOT stamp a binding terminal_signal (§3.6 row: terminal_signal=None); "
        "the death class lives in the coordinator_died run-ledger event + the escalation kind"
    )
    assert any(ev.get("event") == "coordinator_died" for ev in ledger.load_wal()), (
        "coordinator-died must append a coordinator_died run-ledger EVENT"
    )
    assert rb["lease_epoch"] > coord["lease_epoch"], "coordinator-died must bump lease_epoch"


def test_restart_recorded_terminal_is_left_exactly_once(runtime):
    """§5.1 branch 4 — recorded-TERMINAL (done/failed/dead) -> LEAVE: reconcile-EXACTLY-once, NO second
    action and NO second append.

    Mutant killed: re-act on a terminal binding -> a second necro / a second WAL row on the re-run.
    Exactly-once is asserted by RUNNING reconcile twice and checking the terminal binding is byte-for-
    byte unchanged AND the WAL grew by ZERO rows across BOTH passes.
    """
    b = _binding(TERM_NODE, level="L5", state="dead", generation=8,
                 terminal_signal="DIED_INFRA", tmux_target="harness:" + TERM_NODE)
    b["terminal_signal_at"] = "2026-06-05T10:00:00+00:00"
    _seed(b)
    before_binding = copy.deepcopy(ledger.read_binding(TERM_NODE))
    wal_before = len(ledger.load_wal())
    tmux = FakeTmux({})  # tmux absent — but a terminal node must be LEFT regardless

    report1 = reconcile.reconcile_on_restart(executor, tmux)
    after_first = copy.deepcopy(ledger.read_binding(TERM_NODE))
    wal_after_first = len(ledger.load_wal())

    report2 = reconcile.reconcile_on_restart(executor, tmux)
    after_second = ledger.read_binding(TERM_NODE)
    wal_after_second = len(ledger.load_wal())

    assert TERM_NODE not in _necroed(report1), "a recorded-terminal node must NOT be necro'd (it is LEFT)"
    assert TERM_NODE not in _necroed(report2), "a recorded-terminal node must NOT be necro'd on re-run"
    assert after_first == before_binding, "first reconcile must LEAVE a terminal binding untouched"
    assert after_second == before_binding, "second reconcile must LEAVE the terminal binding untouched"
    assert wal_after_first == wal_before, "reconcile-exactly-once: NO WAL append for a terminal node (pass 1)"
    assert wal_after_second == wal_before, "reconcile-exactly-once: NO second WAL append on the re-run"


def test_restart_orphan_tmux_present_no_binding_escalates(runtime):
    """§5.1 branch 5 — tmux-present & NO binding (alive-but-unowned) -> ESCALATE orphan.

    Mutant killed: ignore a tmux target that has no binding -> the orphan is silently dropped instead
    of escalated to cluster-2 / L1.
    """
    # Seed ONE ordinary owned node so the sweep has a binding map, plus a tmux target with NO binding.
    owned = _binding(ADOPT_NODE, level="L5", state="running", session_uuid="sess-own",
                     tmux_target="harness:" + ADOPT_NODE)
    _seed(owned)
    tmux = FakeTmux({
        "harness:" + ADOPT_NODE: _target_alive(),   # the owned node, alive
        ORPHAN_TARGET: _target_alive(pid=9999),     # alive-but-unowned -> orphan
    })

    report = reconcile.reconcile_on_restart(executor, tmux)

    escalated = _escalated_nodes(report)
    assert any(ORPHAN_TARGET == e or (e is not None and ORPHAN_TARGET in str(e)) for e in escalated) \
        or any(ORPHAN_TARGET in str(esc) for esc in _escalations(report)), (
        "an alive-but-unowned tmux target (no binding) must be ESCALATED as an orphan"
    )


def test_restart_pane_dead_leaf_is_necroed_like_tmux_absent(runtime):
    """§5.1 — 'tmux-absent OR pane_dead' are EQUIVALENT for a leaf: a present-but-pane_dead pane is
    owned-but-dead and must necro, exactly like a gone pane.

    Mutant killed: treat pane_dead==1 as 'present/alive' (only checks key presence in list_targets)
    -> a crashed-but-not-yet-reaped leaf pane is wrongly adopted instead of necro'd.
    """
    b = _binding(LEAF_NODE, level="L5", state="running", generation=4, lease_epoch=2,
                 tmux_target="harness:" + LEAF_NODE)
    _seed(b)
    tmux = FakeTmux({"harness:" + LEAF_NODE: _target_dead()})  # PRESENT but pane_dead=1

    report = reconcile.reconcile_on_restart(executor, tmux)

    assert LEAF_NODE in _necroed(report), "a pane_dead leaf is owned-but-dead -> must be necro'd"
    assert LEAF_NODE not in _adopted(report), "a pane_dead pane must NOT be adopted"
    assert ledger.read_binding(LEAF_NODE)["state"] == "failed", (
        "pane_dead leaf must reach 'failed' (§3.6 maps DIED_INFRA -> state failed)"
    )


def test_restart_blocked_leaf_necro_lands_failed(runtime):
    """A BLOCKED leaf whose pane is gone necros to 'failed' through the REAL executor — proving the
    blocked→failed legality fold (§3.6 died_* classes; review reconcile-1) is actually traversable.

    Mutant killed: drop the {non-terminal}→failed fold from states.ALLOWED_TRANSITIONS -> the §4.2
    legality gate aborts the necro CAS -> the blocked leaf never reaches failed -> caught here.
    """
    b = _binding(
        LEAF_NODE, level="L5", state="blocked", generation=4, lease_epoch=2,
        tmux_target="harness:" + LEAF_NODE,
    )
    _seed(b)
    wal_before = len(ledger.load_wal())
    tmux = FakeTmux({})  # the pane is GONE -> owned-but-dead

    report = reconcile.reconcile_on_restart(executor, tmux)

    assert LEAF_NODE in _necroed(report), "a dead BLOCKED leaf must be reported as necro'd"
    rb = ledger.read_binding(LEAF_NODE)
    assert rb["state"] == "failed", (
        "§3.6 maps DIED_INFRA -> state failed: a blocked leaf-necro must land 'failed' (the "
        "blocked→failed fold must be a legal edge — a legality-gate abort here means the fold is missing)"
    )
    assert rb.get("terminal_signal") == "DIED_INFRA", "the necro must stamp the DIED_INFRA death class"
    assert rb["lease_epoch"] > b["lease_epoch"], "the necro must BUMP lease_epoch (fence the prior)"
    death_rows = [
        r for r in ledger.load_wal()[wal_before:]
        if r.get("node_address") == LEAF_NODE and r.get("event") == "died_infrastructure"
    ]
    assert len(death_rows) == 1, (
        "exactly ONE died_infrastructure WAL row must be appended for the blocked-leaf necro; "
        f"got {len(death_rows)}"
    )


def test_necro_transition_abort_is_escalated_not_swallowed(runtime):
    """A leaf-necro whose terminal write ABORTS (the executor returns ok=False) must surface as an
    ESCALATION (kind='necro_failed'), never appear in report.necroed as if it committed.

    Mutant killed: the result-swallowing call (`executor.transition(...)` with no routing) — the sweep
    reports the node necro'd while the on-disk binding never moved (a silent lie to cluster-②).
    """
    b = _binding(
        LEAF_NODE, level="L5", state="running", generation=4, lease_epoch=2,
        tmux_target="harness:" + LEAF_NODE,
    )
    _seed(b)
    tmux = FakeTmux({})  # pane gone -> the sweep tries the necro

    class _AbortingExecutor:
        """Wrap the REAL executor but force transition() to an ok=False abort (a CAS-miss shape)."""

        def __getattr__(self, name):
            return getattr(executor, name)

        def transition(self, node_address, **kwargs):
            live = ledger.read_binding(node_address)
            return executor.TransitionResult(
                ok=False,
                errors=["forced abort: simulated CAS miss on the necro terminal write"],
                warnings=[],
                binding=live,
            )

    report = reconcile.reconcile_on_restart(_AbortingExecutor(), tmux)

    assert LEAF_NODE not in _necroed(report), (
        "an ABORTED necro write must NOT be reported in report.necroed (that would claim a commit "
        "that never landed)"
    )
    necro_escalations = [
        esc for esc in _escalations(report)
        if isinstance(esc, dict) and esc.get("node_address") == LEAF_NODE
        and esc.get("kind") == "necro_failed"
    ]
    assert necro_escalations, (
        "an aborted leaf-necro must surface as an escalation with kind='necro_failed' "
        f"(got escalations={_escalations(report)!r})"
    )
    assert any(esc.get("errors") for esc in necro_escalations), (
        "the necro_failed escalation must carry the abort errors so cluster-② can route the cause"
    )


def test_restart_adopt_refused_when_uuid_mismatches(runtime):
    """§5.1 branch 1 guard — tmux-present but session_uuid does NOT match the binding -> NOT an adopt.

    A present pane whose uuid differs from the recorded one is a DIFFERENT incarnation; adopting it
    would resume ownership of the wrong process. Mutant killed: adopt on pane-presence ALONE (ignore
    the session_uuid match) -> ownership is resumed over an impostor pane.
    """
    b = _binding(ADOPT_NODE, level="L5", state="running",
                 session_uuid="sess-recorded", tmux_target="harness:" + ADOPT_NODE)
    _seed(b)
    # The pane is present, but the live session_uuid differs from what the binding recorded.
    tmux = FakeTmux({
        "harness:" + ADOPT_NODE: {"pane_pid": 4321, "pane_dead": 0, "window_activity": "0",
                                  "session_uuid": "sess-DIFFERENT"},
    })

    report = reconcile.reconcile_on_restart(executor, tmux)

    assert ADOPT_NODE not in _adopted(report), (
        "a uuid-MISMATCHED present pane must NOT be silently adopted (it is a different incarnation)"
    )
