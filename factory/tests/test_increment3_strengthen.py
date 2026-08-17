"""Increment 3 — load-bearing STRENGTHENING (mutation-review gate).

Gap the review flagged: check_fence's epoch credential is EQUALITY (reject if DIFFERENT), not
merely "reject if LOWER". The existing higher-epoch test presents a mismatching *token* that rejects
first, so the epoch branch is never isolated on the high side — a mutant using
`expected_lease_epoch < binding['lease_epoch']` (reject-only-if-lower) survives all 24 tests.
A forged FUTURE epoch must be rejected too: the live binding is the sole ownership authority, not
largest-number-wins (DAEMON §4.2 / §8). Isolate the epoch-equality on the high side with a None token.
"""

import harnessd.fencing as fencing


def _binding(epoch: int) -> dict:
    return {
        "owner_token": fencing.mint_owner_token("proj/x#exec", "sa-1", "uuid-1", epoch),
        "lease_epoch": epoch,
    }


def test_check_fence_rejects_higher_than_live_epoch_in_isolation():
    """A presented epoch HIGHER than the live binding's is rejected (equality, not 'only if lower').
    Token=None isolates the epoch branch, so a `<`-instead-of-`!=` mutant is caught here."""
    binding = _binding(4)
    assert fencing.check_fence(binding, None, 5) is False, (
        "a forged future epoch (5 > live 4) must be rejected — the live binding is the sole "
        "authority, not largest-number-wins"
    )


def test_check_fence_accepts_exact_epoch_with_none_token():
    """Control: the matching epoch with a None token passes (proves the rejection above is the
    high-side equality, not a blanket 'None token always rejects')."""
    binding = _binding(4)
    assert fencing.check_fence(binding, None, 4) is True


def test_check_fence_rejects_lower_epoch_in_isolation():
    """The low-side, isolated with a None token (kills a 'reject-only-if-higher' mutant too)."""
    binding = _binding(4)
    assert fencing.check_fence(binding, None, 3) is False
