"""Increment 7 — load-bearing STRENGTHENING (mutation-review gate + bias-to-real).

Gaps the reviews flagged:
  1. (surviving mutant) the seq WATERMARK guard wasn't isolated — the existing below-watermark test had an
     event whose pre-image also didn't match, so disabling the watermark guard kept all tests green. Pin it
     with a below-watermark event whose pre-image WOULD match, so ONLY the watermark guard prevents a
     state-corrupting re-apply (generation/watermark going backwards).
  2. (MEDIUM, fixed) replay silently dropped a WAL event for a node ABSENT from the binding checkpoint
     (first-write-crashed). Now reconstructed from the event chain — pin it.
  3. (too-loose) escalation payloads carry a load-bearing `kind` (orphan vs coordinator_died) cluster-2
     routes on; assert it.
"""

import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.reconcile as reconcile


# Reuse the real builders directly (these tests are pure-function + real-on-disk).

def _rec(seq, expected_generation, generation, *, node="proj/w#exec", from_state="running",
         to_state="running", delta=None):
    return ledger.build_wal_record(
        node_address=node, event="state_transition", from_state=from_state, to_state=to_state,
        expected_generation=expected_generation, generation=generation, lease_epoch=3,
        owner_token=fencing.mint_owner_token(node, "sa", "uuid", 3),
        binding_delta=(delta or {}), summary="s", artifacts=[], seq=seq,
    )


def _bind(node, *, generation, last_applied_seq, state="running"):
    return {"node_address": node, "state": state, "generation": generation,
            "last_applied_seq": last_applied_seq, "owner_token": "t", "lease_epoch": 3}


# --- watermark guard isolated: a below-watermark event whose PRE-IMAGE matches must be skipped ----

def test_below_watermark_event_with_matching_preimage_is_skipped():
    """binding gen=5, watermark=10; a WAL event at seq=8 (BELOW watermark) whose expected_generation==5
    (pre-image MATCHES). WITHOUT the watermark guard it would re-apply (gen->6, watermark backwards to 8)
    — corruption. The watermark guard is the ONLY thing that prevents it."""
    node = "proj/w#exec"
    bindings = {node: _bind(node, generation=5, last_applied_seq=10)}
    stale = _rec(8, expected_generation=5, generation=6, node=node, delta={"state": "blocked"})
    out = reconcile.replay_wal(bindings, [stale])
    assert out[node]["generation"] == 5, "a below-watermark event must NOT re-apply, even with matching pre-image"
    assert out[node]["last_applied_seq"] == 10, "the watermark must NOT move backwards"
    assert out[node]["state"] == "running", "the stale below-watermark delta must NOT be applied"


# --- MEDIUM: a WAL-only node (absent from the binding checkpoint) is RECONSTRUCTED, not dropped -----

def test_wal_only_node_is_reconstructed_not_dropped():
    """A node present in the WAL but ABSENT from the binding map (first binding write crashed) is
    reconstructed from its event chain (WAL is the source of truth), not silently dropped."""
    node = "proj/orphanwal#exec"
    # base pre-image: planned@gen0 -> the chain claims it to claimed@gen1
    ev = _rec(1, expected_generation=0, generation=1, node=node,
              from_state="planned", to_state="claimed", delta={"state": "claimed"})
    out = reconcile.replay_wal({}, [ev])  # empty binding map; node exists ONLY in the WAL
    assert node in out, "a WAL-only node must be reconstructed, not silently dropped"
    assert out[node]["state"] == "claimed", "reconstruct must apply the event chain from the pre-image base"
    assert out[node]["generation"] == 1
    assert out[node]["last_applied_seq"] == 1


def test_wal_only_reconstruction_is_idempotent():
    """Reconstructing a WAL-only node twice yields the same result (replay determinism holds for the
    reconstructed path too)."""
    node = "proj/orphanwal#exec"
    ev = _rec(1, expected_generation=0, generation=1, node=node,
              from_state="planned", to_state="claimed", delta={"state": "claimed"})
    once = reconcile.replay_wal({}, [ev])
    twice = reconcile.replay_wal(once, [ev])  # feed the reconstructed map back in
    assert twice[node]["generation"] == 1, "reconstructed node must be idempotent under re-replay"
    assert twice[node]["last_applied_seq"] == 1
