"""F6 — reconcile's replay checkpoint writes inside the REAL per-mutation EX lock. Findings
reconcile-2 / SWCAS-01.

The defect: ``reconcile._reconcile``'s boot-only replay block called
``ledger.write_binding(replayed, _lock_held=True)`` with NO surrounding lock — the flag claimed a
held EX lock that nothing held (genesis's brief STEP1+2 acquire releases before STEP 4 runs the
reconcile). The fix: the replay block wraps its WAL-load -> replay -> checkpoint-write in
``store.file_lock(executor.lock_path(), shared=False)`` — ONE critical section inside the real
§4.3 per-mutation lock, making the flag true-by-fact. The lock RELEASES before the classification
sweep: the executor re-takes the same lock per mutation and fcntl flock is not re-entrant, so a
wider scope would self-deadlock.

This file is the F-fix pattern of a NEW dedicated test file (cf. test_replay_to_state.py):
tests/test_reconcile.py and tests/test_integration_c.py are FROZEN acceptance files and stay
byte-stable.

BIAS TO REAL (Lesson 7): REAL on-disk WAL + binding ledgers under a tmp RUNTIME_ROOT (real
``ledger.build_wal_record`` / ``append_wal`` / ``write_binding`` / ``load_wal``), the REAL executor
(the single writer), real flock. Test 1's only seams are DELEGATING spies on ``store.file_lock``
(calls the real contextmanager, flips a per-path held-flag while inside — the test_genesis.py
poison-the-seam precedent, made transparent) and ``ledger.write_binding`` (records, at call time,
whether the EX held-flag for ``executor.lock_path()`` is True, then delegates). Test 2 has NO
monkeypatch at all.

LOAD-BEARING (each test kills a named mutant):
  * the replay checkpoint write happens with the EX lock held BY FACT (mutant: the pre-fix bare
    ``_lock_held=True`` flag-only call — held-flag False at write time)
  * the replay lock releases BEFORE the sweep (mutant: widening the with-block over the
    classification sweep -> executor.transition flock-deadlocks -> the test hangs/never returns)
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path

import pytest

import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.reconcile as reconcile
import harnessd.states as states
import harnessd.store as store


# ---------------------------------------------------------------------------
# Runtime fixture + helpers — mirror tests/test_reconcile.py exactly (the
# established reconcile pair): real artifacts, minimal FakeTmux.
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


class FakeTmux:
    """The ONLY faked runtime surface: list_targets() -> {target: {pane_pid, pane_dead, ...}}."""

    def __init__(self, targets):
        self._targets = targets

    def list_targets(self):
        return dict(self._targets)


NODE = "proj/widget#exec"
DEAD_LEAF = "proj/widget#qa"


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
    """One full-shape binding (DAEMON §3 schema) — the test_reconcile.py helper, verbatim."""
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


def _pending_wal_record(*, node_address=NODE, seq, expected_generation, generation, owner_token,
                        binding_delta, from_state="spawning", to_state="running", lease_epoch=3):
    """One REAL WAL record via the real builder (carries crc32 + the transition pre/post image)."""
    return ledger.build_wal_record(
        node_address=node_address,
        event="state_transition",
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_generation,
        generation=generation,
        lease_epoch=lease_epoch,
        owner_token=owner_token,
        binding_delta=binding_delta,
        summary="replay-lock-test",
        artifacts=[],
        seq=seq,
    )


POST_TOKEN = "proj/widget#exec:sub:sess-aaaa-0001:3"


def _seed_pending_replay():
    """Seed the §4.4 crash window on disk: binding one generation BEHIND its appended WAL row.

    Returns the seeded binding. The pending record (seq=5 > last_applied_seq=0, pre-image
    expected_generation=4 == the live generation) is exactly what reconcile-on-restart must replay.
    """
    binding = _binding(NODE, level="L5", state="spawning", generation=4, last_applied_seq=0)
    pending = _pending_wal_record(
        seq=5,
        expected_generation=4,   # pre-image MATCHES the live binding.generation
        generation=5,            # post-commit generation
        owner_token=POST_TOKEN,
        binding_delta={"state": "running"},
    )
    ledger.append_wal(pending)
    return binding


# ===========================================================================
# The replay checkpoint write happens INSIDE the real per-mutation EX lock.
# ===========================================================================

def test_replay_checkpoint_writes_inside_the_real_EX_lock(runtime, monkeypatch):
    binding = _seed_pending_replay()
    _seed(binding)

    ex_lock_path = Path(executor.lock_path())

    # DELEGATING spy on store.file_lock: runs the REAL contextmanager (a real flock is taken and
    # released) and flips a per-path EX held-flag while inside — the transparent version of the
    # test_genesis.py poison-this-seam precedent.
    real_file_lock = store.file_lock
    ex_held: dict = {}

    @contextmanager
    def spy_file_lock(path, *, shared):
        with real_file_lock(path, shared=shared):
            key = Path(path)
            if not shared:
                ex_held[key] = ex_held.get(key, 0) + 1
            try:
                yield
            finally:
                if not shared:
                    ex_held[key] = ex_held.get(key, 0) - 1

    monkeypatch.setattr(store, "file_lock", spy_file_lock)

    # DELEGATING spy on ledger.write_binding: record, AT CALL TIME, whether the EX lock for the
    # executor's lock path is held by fact, then delegate to the real writer.
    real_write_binding = ledger.write_binding
    writes: list = []

    def spy_write_binding(candidate_map, *, _lock_held, **kwargs):
        writes.append(
            {
                "ex_held_by_fact": ex_held.get(ex_lock_path, 0) > 0,
                "flagged_lock_held": _lock_held,
                "nodes": sorted(candidate_map),
            }
        )
        return real_write_binding(candidate_map, _lock_held=_lock_held, **kwargs)

    monkeypatch.setattr(ledger, "write_binding", spy_write_binding)

    # The replayed node's pane is ALIVE (no uuid contradiction) -> the sweep ADOPTs it, so the
    # replay checkpoint is the ONLY whole-map write this run performs.
    report = reconcile.reconcile_on_restart(executor, FakeTmux({binding["tmux_target"]: _target_alive()}))

    # The checkpoint happened, exactly once, and it carried the replayed node.
    assert writes, "reconcile_on_restart must persist the replayed map (the recovery checkpoint)"
    checkpoint = writes[0]
    assert NODE in checkpoint["nodes"], "the recovery checkpoint must carry the replayed node"
    assert checkpoint["flagged_lock_held"] is True, (
        "the checkpoint rides the §2.10-sanctioned write_binding(..., _lock_held=True) path"
    )
    # THE fix (reconcile-2 / SWCAS-01): the flag must be TRUE-BY-FACT — the real per-mutation EX
    # lock (executor.lock_path()) is held at the instant of the write. Mutant killed: the pre-fix
    # bare flag-only call (no surrounding lock) records ex_held_by_fact == False here.
    assert checkpoint["ex_held_by_fact"], (
        "the replay checkpoint must write INSIDE the real held EX lock (store.file_lock on "
        "executor.lock_path(), shared=False) — _lock_held=True was flag-only-false before F6"
    )

    # And the replayed binding is durably on disk (post-commit image via the real ledger).
    on_disk = ledger.read_binding(NODE)
    assert on_disk["generation"] == 5, "replay must advance generation to the POST-commit value"
    assert on_disk["owner_token"] == POST_TOKEN, "replay must set the post-commit owner_token"
    assert on_disk["last_applied_seq"] == 5, "replay must stamp the watermark (last_applied_seq=seq)"
    assert on_disk["state"] == "running", "replay must land the record's authoritative to_state"
    assert NODE in list(report.adopted), "the live replayed node is then ADOPTED by the sweep"


# ===========================================================================
# The replay lock releases BEFORE the sweep — no flock self-deadlock with the
# executor's per-mutation acquire. Loop-level, NO monkeypatch.
# ===========================================================================

def test_replay_lock_releases_before_the_sweep_no_deadlock(runtime):
    # (a) Replay work: a binding one generation behind its appended WAL row.
    replay_binding = _seed_pending_replay()
    # (b) Necro work: a running LEAF whose tmux_target is ABSENT from the listing — the sweep must
    # drive it terminal through the REAL executor, which RE-TAKES the per-mutation EX lock. If the
    # replay block's lock were still held across the sweep, that acquire would flock-deadlock and
    # this test would never return (mutant: widening the with-block over the classification sweep).
    dead_leaf = _binding(DEAD_LEAF, level="L5", state="running", generation=7, lease_epoch=3,
                         session_uuid="sess-dead-0001")
    _seed(replay_binding, dead_leaf)

    # The replayed node's pane is alive (adopt); the dead leaf's target is ABSENT (necro).
    tmux = FakeTmux({replay_binding["tmux_target"]: _target_alive()})

    report = reconcile.reconcile_on_restart(executor, tmux)  # must RETURN — no self-deadlock

    # The necro committed through the real executor: §3.6 death-class state on disk + bumped epoch.
    assert DEAD_LEAF in list(report.necroed), (
        "the owned-but-dead leaf must be necroed by the sweep AFTER the replay lock released"
    )
    on_disk_leaf = ledger.read_binding(DEAD_LEAF)
    died_infra_state = states.TERMINAL_VOCAB["died_infrastructure"].state  # "failed" (§3.6, post-F5)
    assert on_disk_leaf["state"] == died_infra_state and states.is_terminal(on_disk_leaf["state"]), (
        "the leaf-necro must land the §3.6 death-class state via the executor (a mutation that "
        "requires the per-mutation EX lock to be FREE after replay)"
    )
    assert on_disk_leaf["lease_epoch"] == 3 + 1, "the necro must bump lease_epoch (fence the prior incarnation)"

    # And the replay itself still landed: generation + watermark advanced on disk.
    on_disk_replayed = ledger.read_binding(NODE)
    assert on_disk_replayed["generation"] == 5 and on_disk_replayed["last_applied_seq"] == 5, (
        "the boot-replay checkpoint must have advanced the replayed node before the sweep"
    )
    assert NODE in list(report.adopted), "the live replayed node is adopted in the same pass"
