"""Increment 3 — fencing (FROZEN acceptance, RED-first).

The split-brain defense (F-024 / F-012). A stale actor returning after a respawn must be
de-authorized SILENTLY and NON-DESTRUCTIVELY: its presented credentials lose to the live
binding's, the mutation is refused, and the live binding is left UNCHANGED (the caller journals
`stale_return_ignored`; the old actor is de-authed, NOT auto-killed).

Authoritative (grounded, not recalled):
  * IMPLEMENTATION-PLAN §2.5 — the frozen fencing.py interface (exact signatures):
        mint_owner_token(node_address, subagent_id, session_uuid, lease_epoch) -> str
            # composite 'address:subagent-id:session-uuid:lease_epoch' — EMBEDS the epoch =>
            # SELF-FENCING (comparing two tokens compares their epochs).
        advance_epoch(binding) -> int                      # binding's lease_epoch + 1
        check_fence(binding, expected_owner_token, expected_lease_epoch) -> bool
            # True iff the presented credentials still own the live binding. Stale (lower epoch)
            # -> False; fresh (current) -> True. Token comparison reduces to epoch comparison.
  * DAEMON §8 — the fencing MECHANISM (lease_epoch monotonic-per-node bumped on every
    claim/adopt/respawn/transfer; owner_token composite self-fencing; stale-return non-destructive).
  * DAEMON §3.2 — the binding-ledger schema (the binding dict these pure functions read):
        lease_epoch: int, owner_token: "address:subagent-id:session-uuid:lease_epoch", ...

These are PURE functions over a binding dict — no I/O, stdlib only. They build on nothing but the
binding schema. The §2.6 executor folds check_fence in as its 3rd CAS precondition; here we pin the
primitive in isolation.

LOAD-BEARING DESIGN (each test is calibrated to KILL a specific wrong impl):
  * check_fence rejects a lower-epoch token            -> kills the accept-anything mutant.
  * check_fence accepts a matching (current) token     -> kills the reject-anything mutant.
  * advance_epoch == old + 1                            -> kills the "return old" mutant (a fresh
        post-advance token would TIE the stale one, and the split-brain would not be fenced).
  * mint_owner_token EMBEDS the epoch in the composite -> kills the "drop the epoch" mutant (two
        tokens differing ONLY in epoch would otherwise COLLIDE and a stale return would pass).

RED until harnessd/fencing.py exists (tests-only increment — NO implementation written here).
"""

import importlib

import pytest

# RED-FIRST (deliberate): a plain import — NOT pytest.importorskip — so that an absent
# harnessd/fencing.py produces a hard collection FAILURE, not a green SKIP. This is a
# tests-only increment; the whole point of "confirm RED" is that the suite must be RED
# until the builder writes the module. A skip would be a false-green that hides the gap.
import harnessd.fencing as fencing


# ======================================================================================
# Fixtures / helpers — a DAEMON §3.2 binding dict, parameterized by epoch + token.
# ======================================================================================

NODE = "payments/gateway/stripe-client#exec"
SUBAGENT = "subagent-0a1b2c3d"
SESSION = "3f2a9c81-0000-4abc-9def-111122223333"


def _token(node=NODE, subagent=SUBAGENT, session=SESSION, epoch=3):
    """Mint via the SUT so the binding's owner_token and the presented token share one format."""
    return fencing.mint_owner_token(node, subagent, session, epoch)


def _binding(*, lease_epoch=3, owner_token=None, node=NODE, subagent=SUBAGENT, session=SESSION):
    """A live binding record (DAEMON §3.2 subset) with epoch + owner_token coherently set."""
    if owner_token is None:
        owner_token = _token(node=node, subagent=subagent, session=session, epoch=lease_epoch)
    return {
        "node_address": node,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        # carried §3.2 fields the fence MUST NOT depend on (presence, not interpretation):
        "state": "running",
        "generation": 7,
        "session_uuid": session,
    }


# ======================================================================================
# A. mint_owner_token — the composite FORMAT, with the epoch EMBEDDED (self-fencing).
#    LOAD-BEARING: a mint that DROPS the epoch from the composite collides two
#    epoch-distinct tokens -> caught here.
# ======================================================================================

def test_mint_owner_token_is_the_exact_composite_format():
    """§2.5 / §8: 'address:subagent-id:session-uuid:lease_epoch' — exactly, in that order."""
    tok = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 3)
    assert tok == f"{NODE}:{SUBAGENT}:{SESSION}:3", (
        "owner_token must be the composite 'address:subagent-id:session-uuid:lease_epoch'"
    )


def test_mint_owner_token_address_is_first_field_not_role():
    """§8 recovered format was role:...; v1 maps role -> the one-spine ADDRESS as field 0."""
    tok = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 3)
    assert tok.split(":", 1)[0] == NODE, "the leading field of the composite is the one-spine address"


def test_mint_owner_token_embeds_the_epoch_as_the_trailing_field():
    """The epoch is the LAST colon-field. (Self-fencing depends on the epoch being IN the token.)"""
    tok = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 7)
    assert tok.endswith(":7"), "the lease_epoch must be embedded as the trailing composite field"
    assert tok.rsplit(":", 1)[1] == "7", "trailing composite field must be exactly the epoch"


def test_mint_owner_token_epoch_is_load_bearing_two_epochs_must_differ():
    """LOAD-BEARING (kills 'mint drops the epoch'): two tokens identical in node/subagent/session
    but minted at DIFFERENT epochs MUST be different strings. A mint that omits the epoch would
    collide these, and a stale return would then pass the fence."""
    lo = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 3)
    hi = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 4)
    assert lo != hi, (
        "tokens differing only in epoch must NOT collide — the epoch must be embedded "
        "(else a stale prior-epoch actor's token would tie the live one and bypass the fence)"
    )


def test_mint_owner_token_same_inputs_are_deterministic():
    """Self-fencing requires that the live binding's stored token and a freshly minted one for the
    SAME (node, subagent, session, epoch) compare equal — no nonce/timestamp inside."""
    a = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 5)
    b = fencing.mint_owner_token(NODE, SUBAGENT, SESSION, 5)
    assert a == b, "mint must be a pure function of its four inputs (no hidden entropy)"


# ======================================================================================
# B. advance_epoch — monotonic, old + 1.
#    LOAD-BEARING: a mutant that returns `old` (no bump) would let a post-advance token TIE
#    the stale one -> the respawn would not fence the prior incarnation -> caught here.
# ======================================================================================

def test_advance_epoch_is_old_plus_one():
    """§2.5: advance_epoch(binding) == binding.lease_epoch + 1."""
    assert fencing.advance_epoch(_binding(lease_epoch=3)) == 4


@pytest.mark.parametrize("old", [0, 1, 3, 41, 9999])
def test_advance_epoch_is_strictly_old_plus_one_across_values(old):
    """LOAD-BEARING (kills 'return old' and 'return constant'): the result is EXACTLY old+1, and in
    particular strictly GREATER than old, for every starting epoch."""
    nxt = fencing.advance_epoch(_binding(lease_epoch=old))
    assert nxt == old + 1, "advance_epoch must be exactly old + 1 (monotonic single-step bump)"
    assert nxt > old, "advance_epoch must strictly increase the epoch (a 'return old' mutant ties)"


def test_advance_epoch_does_not_mutate_the_binding():
    """Pure function: computing the next epoch must not write back into the live binding (the
    rotation lands in the §2.6 candidate, in the same atomic transaction, not here)."""
    b = _binding(lease_epoch=3)
    fencing.advance_epoch(b)
    assert b["lease_epoch"] == 3, "advance_epoch must be side-effect free on the input binding"


# ======================================================================================
# C. check_fence — stale REJECTED / fresh ACCEPTED / token reduces to epoch.
#    These two are the central mutation-kill pair:
#      reject a stale token  -> kills accept-anything (always True).
#      accept a fresh token  -> kills reject-anything (always False).
# ======================================================================================

def test_check_fence_accepts_the_current_token_and_epoch():
    """FRESH credentials (the live binding's own token + epoch) -> True.
    LOAD-BEARING: kills the reject-anything (always-False) mutant."""
    b = _binding(lease_epoch=3)
    assert fencing.check_fence(b, b["owner_token"], b["lease_epoch"]) is True


def test_check_fence_rejects_a_lower_epoch_token():
    """A STALE actor: a token minted at a LOWER epoch than the live binding's -> False.
    LOAD-BEARING: kills the accept-anything (always-True) mutant — the headline split-brain case."""
    b = _binding(lease_epoch=4)                       # live binding has advanced to epoch 4
    stale_token = _token(epoch=3)                      # prior incarnation, epoch 3
    assert fencing.check_fence(b, stale_token, 3) is False, (
        "a prior-epoch (stale) owner_token must be rejected — this is the F-024 fence"
    )


def test_check_fence_rejects_a_lower_epoch_argument():
    """Token comparison reduces to epoch comparison: even framed via the explicit epoch arg, a
    lower presented epoch than the live binding's is stale -> False."""
    b = _binding(lease_epoch=5)
    assert fencing.check_fence(b, _token(epoch=2), 2) is False


def test_check_fence_token_comparison_reduces_to_epoch_comparison():
    """The DONE-TEST clause: a token minted at a LOWER epoch loses to one minted at a HIGHER epoch
    for the SAME node/subagent/session. We drive both sides through mint so the ONLY difference is
    the epoch — proving the decision is the epoch, embedded in the token."""
    live_epoch, stale_epoch = 6, 5
    b = _binding(lease_epoch=live_epoch)               # binding owner_token minted at epoch 6
    fresh_token = _token(epoch=live_epoch)             # epoch 6 — current
    stale_token = _token(epoch=stale_epoch)            # epoch 5 — prior incarnation, same node/sub/session
    assert fresh_token != stale_token, "precondition: the two tokens differ only by embedded epoch"
    assert fencing.check_fence(b, fresh_token, live_epoch) is True, "higher (current) epoch wins"
    assert fencing.check_fence(b, stale_token, stale_epoch) is False, "lower (prior) epoch loses"


def test_check_fence_rejects_a_token_for_a_different_session():
    """Self-fencing identity: a token for a DIFFERENT session at the SAME epoch is not this owner.
    (A respawn mints a NEW session_uuid; the prior incarnation's token must not validate even if
    epochs happened to coincide.)"""
    b = _binding(lease_epoch=3, session=SESSION)
    other = _token(session="ffffffff-0000-0000-0000-000000000000", epoch=3)
    assert other != b["owner_token"], "precondition: distinct sessions mint distinct tokens"
    assert fencing.check_fence(b, other, 3) is False, (
        "a token from a different incarnation (session) must not own the live binding"
    )


def test_check_fence_non_destructive_leaves_binding_unchanged_on_reject():
    """§8 non-destructive de-authorization: a rejected (stale) check must NOT mutate the live
    binding — the caller journals stale_return_ignored and leaves ownership intact. check_fence is
    a pure predicate; it must not alter epoch/token as a side effect of returning False."""
    b = _binding(lease_epoch=4)
    snapshot = dict(b)
    fencing.check_fence(b, _token(epoch=3), 3)         # stale -> False
    assert b == snapshot, "check_fence must not mutate the binding (non-destructive de-auth)"


def test_check_fence_returns_a_bool():
    """The frozen signature returns bool (the §2.6 executor branches on it as a CAS precondition)."""
    b = _binding(lease_epoch=3)
    assert isinstance(fencing.check_fence(b, b["owner_token"], b["lease_epoch"]), bool)
    assert isinstance(fencing.check_fence(b, _token(epoch=1), 1), bool)


# --------------------------------------------------------------------------------------
# A HIGHER-than-live presented epoch is NOT a valid owner either: the live binding is the
# single source of truth. Only the token/epoch that EQUALS the live binding owns it. (Guards
# a 'presented >= live' mutant that would let a forged-future token pass.)
# --------------------------------------------------------------------------------------

def test_check_fence_rejects_a_higher_than_live_epoch():
    """Ownership is defined by the LIVE binding, not by 'whoever presents the biggest number'.
    A token whose epoch EXCEEDS the live binding's does not match the live owner_token -> False."""
    b = _binding(lease_epoch=3)
    future = _token(epoch=9)
    assert future != b["owner_token"], "precondition: a future-epoch token differs from the live one"
    assert fencing.check_fence(b, future, 9) is False, (
        "only the token matching the LIVE binding owns it — a higher presented epoch is not authoritative"
    )


# ======================================================================================
# D. None-argument semantics (§8 / §2.6) — 'no credential presented' is NOT a stale rejection.
#    §2.6 executor body: `if expected_owner_token is not None and binding.owner_token !=
#    expected_owner_token: ABORT`. So a None expected_owner_token means the fence is NOT invoked
#    for that credential (it passes). The fence REJECTS stale PRESENTED creds; it does not reject
#    the ABSENCE of a credential. (check_fence is the predicate behind that precondition.)
# ======================================================================================

def test_check_fence_none_token_and_none_epoch_is_not_a_rejection():
    """No credential presented (both None) is the un-fenced internal mutator path (§2.6) -> True.
    A 'None means stale -> False' mutant would wrongly abort every un-fenced internal transition."""
    b = _binding(lease_epoch=3)
    assert fencing.check_fence(b, None, None) is True, (
        "None/None (no fence presented) must pass — it is not a stale-credential rejection"
    )


def test_check_fence_none_token_does_not_reject_on_token_grounds():
    """expected_owner_token is None => the token precondition is not applied (§2.6 guards it with
    `is not None`). With a matching/None epoch the fence must not reject on token grounds."""
    b = _binding(lease_epoch=3)
    # token unchecked; epoch matches the live binding -> owns it.
    assert fencing.check_fence(b, None, 3) is True
    # token unchecked; epoch also None -> nothing presented to reject.
    assert fencing.check_fence(b, None, None) is True


def test_check_fence_stale_epoch_still_rejects_even_when_token_is_none():
    """The epoch credential is independently load-bearing: if a STALE epoch is presented (lower than
    the live binding's), the fence rejects it even with a None token. (A mutant that only ever looks
    at the token would wrongly pass a presented-stale-epoch / None-token call.)"""
    b = _binding(lease_epoch=5)
    assert fencing.check_fence(b, None, 2) is False, (
        "a presented stale epoch (lower than live) is rejected regardless of the token argument"
    )


# ======================================================================================
# E. Import-surface sanity — the frozen §2.5 names exist and are callable.
# ======================================================================================

def test_fencing_exposes_the_frozen_interface():
    """§2.5 surface: exactly these three public names, all callable."""
    mod = importlib.import_module("harnessd.fencing")
    for name in ("mint_owner_token", "advance_epoch", "check_fence"):
        assert hasattr(mod, name), f"fencing.{name} (frozen §2.5 interface) is missing"
        assert callable(getattr(mod, name)), f"fencing.{name} must be callable"
