"""F3 — the spawn-failure taxonomy carries a failure_class (REMEDIATION; review oauth_guard-1/-2,
claude_code-2/-3).

The exceptions (AuthExpired / ApiKeyForbidden) carried NO failure_class attribute, so the chokepoint's
``getattr(exc, "failure_class", None) or "model_unavailable"`` misrouted: an auth-token lapse escalated
as a MODEL OUTAGE (the storm DAEMON §6.3 forbids — "model down, wait" when the truth is "refresh the
token"), and ApiKeyForbidden (NOT a SpawnFailure) leaked UNCAUGHT past the chokepoint, leaving the claim
committed. Fix: each exception carries its class; the chokepoint catches ApiKeyForbidden too.
"""

import copy

import pytest

import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.spawn.chokepoint as chokepoint
import harnessd.spawn.oauth_guard as oauth_guard


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


# --------------------------------------------------------------------------- #
# unit: each exception names its class
# --------------------------------------------------------------------------- #

def test_auth_expired_carries_its_class():
    assert getattr(oauth_guard.AuthExpired("x"), "failure_class", None) == "auth_expired", (
        "AuthExpired must carry failure_class='auth_expired' (else it masquerades as model_unavailable)"
    )


def test_api_key_forbidden_carries_its_class():
    assert getattr(oauth_guard.ApiKeyForbidden("x"), "failure_class", None) == "api_key_forbidden"


# --------------------------------------------------------------------------- #
# integration: the chokepoint routes the right class on a post-claim failure
# --------------------------------------------------------------------------- #

ADDR = "proj/widget/task#exec"


def _seed_planned(runtime, addr=ADDR):
    token = fencing.mint_owner_token(addr, "sa", "uuid", 1)
    rec = {"node_address": addr, "parent_address": "proj/widget#exec", "level": "L5", "subagent_id": "sa",
           "session_uuid": "uuid", "state": "planned", "generation": 0, "lease_epoch": 1,
           "owner_token": token, "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md", "liveness_state": "claimed",
           "tmux_target": "harness:" + addr}
    ledger.write_binding({addr: copy.deepcopy(rec)}, _lock_held=True)
    return token


class _RaisingAdapter:
    def __init__(self, exc):
        self._exc = exc

    def pin_and_open(self, *a, **k):
        raise self._exc


def _install(adapter):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(adapter)
    else:
        chokepoint.ADAPTER = adapter


def _spawn(runtime, token):
    return chokepoint.claim_and_spawn(
        ADDR, expected_state="planned", expected_generation=0,
        expected_owner_token=token, level_config=config.get_level_config("L5"))


def test_auth_lapse_escalates_as_auth_expired_not_model_unavailable(runtime):
    """A post-claim AuthExpired must yield failure_class='auth_expired' on the result + the escalation —
    NOT 'model_unavailable' (the masquerade). The claim is released."""
    token = _seed_planned(runtime)
    _install(_RaisingAdapter(oauth_guard.AuthExpired("token expired")))

    res = _spawn(runtime, token)

    assert getattr(res, "ok", True) is False
    assert getattr(res, "failure_class", None) == "auth_expired", (
        f"an auth lapse must read as auth_expired, not {getattr(res, 'failure_class', None)!r}"
    )


def test_api_key_forbidden_is_caught_and_classed_not_uncaught(runtime):
    """A post-claim ApiKeyForbidden (a hard OAuth-only invariant breach) must be CAUGHT by the chokepoint
    (claim released, escalated as api_key_forbidden) — not leaked uncaught past it (which would crash the
    spawn path AND leave the claim committed)."""
    token = _seed_planned(runtime)
    _install(_RaisingAdapter(oauth_guard.ApiKeyForbidden("raw API key in pane env")))

    # Must NOT raise out of the chokepoint:
    res = _spawn(runtime, token)

    assert getattr(res, "ok", True) is False
    assert getattr(res, "failure_class", None) == "api_key_forbidden", (
        "ApiKeyForbidden must be caught + classed api_key_forbidden, not leaked uncaught"
    )
