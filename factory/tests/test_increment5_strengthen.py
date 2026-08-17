"""Increment 5 — load-bearing STRENGTHENING (mutation-review gate).

Gaps the review flagged:
  1. (coverage) the executor's OWN legality gate isn't isolated: validate.py re-checks is_legal, so
     dropping the executor's inline gate leaves the illegal-target test passing (validate backstops it).
     Neutralize validate so the executor's own gate is the SOLE gate -> a dropped gate is caught.
  2. (MEDIUM correctness, fixed) commit() merges the candidate keyed on candidate['node_address'];
     a binding_delta carrying a foreign node_address must NOT re-key the merge onto another node.
     (Impl now sets candidate['node_address']/['state'] authoritatively from the validated args.)
"""

import pytest

import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.validate as validate


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    return tmp_path


def _seed(node_address, *, state="running", generation=0, epoch=1):
    token = fencing.mint_owner_token(node_address, "sa", "uuid", epoch)
    rec = {
        "node_address": node_address, "state": state, "generation": generation,
        "owner_token": token, "lease_epoch": epoch, "subagent_id": "sa", "session_uuid": "uuid",
    }
    ledger.write_binding({node_address: rec}, _lock_held=True)
    return rec


def test_executor_own_legality_gate_isolated(runtime, monkeypatch):
    """With validate NEUTRALIZED, the executor's own is_legal gate must STILL abort an illegal target."""
    rec = _seed("proj/a#exec", state="running", generation=0)
    monkeypatch.setattr(validate, "validate", lambda candidate, wal_tail: ([], []))
    res = executor.transition(
        "proj/a#exec", expected_state="running", expected_generation=0,
        expected_owner_token=rec["owner_token"], target_state="spawning",  # running->spawning is ILLEGAL
        binding_delta={"state": "spawning"}, event="x",
    )
    assert res.ok is False, "executor's own legality gate must abort even with validate neutralized"
    assert ledger.read_binding("proj/a#exec")["state"] == "running", "binding must be unchanged"


def test_binding_delta_cannot_rekey_node_address(runtime):
    """A binding_delta carrying a foreign node_address must NOT re-key the whole-map merge."""
    rec = _seed("proj/a#exec", state="running", generation=0)
    res = executor.transition(
        "proj/a#exec", expected_state="running", expected_generation=0,
        expected_owner_token=rec["owner_token"], target_state="blocked",
        binding_delta={"state": "blocked", "node_address": "proj/EVIL#exec"}, event="block",
    )
    assert res.ok is True
    landed = ledger.read_binding("proj/a#exec")
    assert landed["state"] == "blocked", "the real node must be updated"
    assert landed["node_address"] == "proj/a#exec", "identity preserved, not overwritten by the delta"
    assert ledger.read_binding("proj/EVIL#exec") is None, "the delta must NOT create/re-key a foreign node"


def test_state_is_target_not_delta(runtime):
    """candidate.state is the legality-checked target_state, even if the delta omits 'state'."""
    rec = _seed("proj/a#exec", state="running", generation=0)
    res = executor.transition(
        "proj/a#exec", expected_state="running", expected_generation=0,
        expected_owner_token=rec["owner_token"], target_state="blocked",
        binding_delta={},  # delta omits state entirely
        event="block",
    )
    assert res.ok is True
    assert ledger.read_binding("proj/a#exec")["state"] == "blocked", "state must follow target_state, not the delta"
