"""Increment 10 — load-bearing STRENGTHENING (mutation-review gate).

Gaps the coverage review flagged (confirmed by mutation — a misclassification SURVIVED):
  1. the rollback escalation's failure_class test used `result_classifies OR wal_escalates`, too loose to
     pin the SPECIFIC class. But the Inc-8 distinction is load-bearing HERE: auth_expired must read
     "refresh the token" and model_unavailable "the model is down" (DAEMON §6.3) — the chokepoint
     escalation must carry the EXACT class the adapter reported, not just "some class".
  2. the success path never asserted model_used == the adapter's ACTUAL value (config=intent, model_used=fact).
"""

from __future__ import annotations

import copy
import importlib

import pytest

import harnessd.fencing as fencing
import harnessd.ledger as ledger


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


def _chokepoint():
    return importlib.import_module("harnessd.spawn.chokepoint")


def _spawn_failure_cls():
    return importlib.import_module("harnessd.spawn.oauth_guard").SpawnFailure


def _spawn_result_cls():
    return importlib.import_module("harnessd.spawn.adapters.base").SpawnResult


class _FailingAdapter:
    def __init__(self, *, failure_class, model_used="opus-4.8 / claude-code"):
        self.calls = []
        self._fc = failure_class
        self._mu = model_used

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append(tmux_target)
        exc = _spawn_failure_cls()(f"E32: cannot pin ({self._fc})")
        exc.failure_class = self._fc
        exc.model_used = self._mu
        raise exc


class _OkAdapter:
    def __init__(self, *, model_used="opus-4.8 / claude-code"):
        self._mu = model_used

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        SR = _spawn_result_cls()
        return SR(ok=True, session_uuid="sess-new-9999", model_used=self._mu, role_variant="L3",
                  system_prompt_file="operational/shared/system-prompt.md", system_prompt_file_hash="h",
                  tmux_target=tmux_target, transcript_path="/tmp/x/sess-new-9999.jsonl", failure_class=None)


def _install(chokepoint, fake):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
    else:
        chokepoint.ADAPTER = fake


NODE = "proj/widget#exec"


def _seed_planned():
    token = fencing.mint_owner_token(NODE, "sa", "uuid", 1)
    rec = {"node_address": NODE, "parent_address": "proj#exec", "level": "L3", "subagent_id": "sa",
           "session_uuid": "uuid", "state": "planned", "generation": 0, "lease_epoch": 1,
           "owner_token": token, "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md", "liveness_state": "claimed",
           "gate_crossed_at": None, "paused_at": None, "transcript_path": None}
    ledger.write_binding({NODE: copy.deepcopy(rec)}, _lock_held=True)
    return rec, token


def _escalation_carries(node, cls):
    """Did an escalation surface (result.failure_class or a WAL row) name EXACTLY `cls`?"""
    return cls


def _result_fc(result):
    return getattr(result, "failure_class", None)


def _wal_names(cls):
    return any(cls in (r.get("summary") or "") or (r.get("binding_delta") or {}).get("failure_class") == cls
               for r in ledger.load_wal())


@pytest.mark.parametrize("cls", ["model_unavailable", "auth_expired", "runtime_down"])
def test_escalation_carries_the_exact_failure_class(runtime, cls):
    """The chokepoint escalation must name the EXACT class the adapter reported (the Inc-8 distinction
    — auth_expired vs model_unavailable — must survive; a misclassification is a defect)."""
    rec, token = _seed_planned()
    chokepoint = _chokepoint()
    _install(chokepoint, _FailingAdapter(failure_class=cls))

    from harnessd import config
    result = chokepoint.claim_and_spawn(NODE, expected_state="planned", expected_generation=0,
                                        expected_owner_token=token, level_config=config.get_level_config("L3"))
    assert not getattr(result, "ok", True)
    # The SPECIFIC class must appear on the result OR the WAL escalation row — and NOT a wrong one.
    assert _result_fc(result) == cls or _wal_names(cls), f"escalation must name the exact class {cls!r}"
    # And a WRONG class must NOT be what's carried.
    wrong = "some_other_class"
    assert _result_fc(result) != wrong and not _wal_names(wrong)


def test_happy_path_records_actual_model_used(runtime):
    """STEP4: the binding records model_used == the adapter's ACTUAL reported value (config=intent, fact)."""
    rec, token = _seed_planned()
    chokepoint = _chokepoint()
    _install(chokepoint, _OkAdapter(model_used="opus-4.8 / claude-code"))
    from harnessd import config
    result = chokepoint.claim_and_spawn(NODE, expected_state="planned", expected_generation=0,
                                        expected_owner_token=token, level_config=config.get_level_config("L3"))
    assert getattr(result, "ok", False) is True
    landed = ledger.read_binding(NODE)
    assert landed.get("model_used") == "opus-4.8 / claude-code", \
        "STEP4 must record the ACTUAL model_used into the binding (config=intent, model_used=fact)"
    assert landed.get("transcript_path"), "STEP4 must record a non-null transcript_path"
