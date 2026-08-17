"""Fencing — the split-brain defense (incident F-024 / F-012).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.5 (the frozen fencing.py interface — exact signatures below).
  - DAEMON §8 (the fencing MECHANISM: ``lease_epoch`` monotonic-per-node bumped on every
    claim/adopt/respawn/transfer; the composite self-fencing ``owner_token``; stale-return
    non-destructive de-authorization).
  - DAEMON §3.2 (the binding-ledger record schema — the binding dict these pure functions read:
    ``lease_epoch: int`` and ``owner_token: "address:subagent-id:session-uuid:lease_epoch"``).

These are PURE functions over a DAEMON §3.2 binding dict — no I/O, stdlib only. The §2.6 executor
folds ``check_fence`` in as its 3rd CAS precondition (``expected_owner_token``); here the primitive
is pinned in isolation.

WHY it is self-fencing (DAEMON §8): the ``owner_token`` embeds the ``lease_epoch`` as its trailing
field, so comparing two tokens reduces to comparing their epochs. A stale actor returning after a
respawn carries a token minted at a LOWER epoch than the live binding's; its credentials lose to the
live binding's, the mutation is REFUSED, and the live binding is left UNCHANGED. The caller journals
``stale_return_ignored`` (the old actor is de-authorized, NOT auto-killed). ``check_fence`` is the
pure predicate behind that precondition — it never mutates the binding.
"""

from __future__ import annotations

# The composite owner_token uses ':' as the field separator (DAEMON §8 / §3.2):
#   address:subagent-id:session-uuid:lease_epoch
_FIELD_SEP = ":"


def mint_owner_token(
    node_address: str,
    subagent_id: str,
    session_uuid: str,
    lease_epoch: int,
) -> str:
    """Mint the composite self-fencing ``owner_token`` (DAEMON §8 / IMPLEMENTATION-PLAN §2.5).

    Returns exactly ``'address:subagent-id:session-uuid:lease_epoch'`` — the four inputs joined by
    ':' in that order, with the ``lease_epoch`` EMBEDDED as the trailing field. Embedding the epoch
    is load-bearing: it makes the token self-fencing (comparing tokens compares epochs), so two
    tokens that differ ONLY in epoch are distinct strings and a stale prior-epoch token cannot tie
    the live one. A pure function of its four inputs — no nonce, no timestamp, no hidden entropy —
    so a freshly minted token compares equal to the binding's stored one for the same inputs.
    """
    return _FIELD_SEP.join(
        (node_address, subagent_id, session_uuid, str(lease_epoch))
    )


def advance_epoch(binding: dict) -> int:
    """Return the binding's next ``lease_epoch`` — exactly ``binding['lease_epoch'] + 1``.

    A single-step monotonic bump (DAEMON §8): strictly greater than the current epoch, so a token
    minted post-advance out-ranks the prior incarnation's and the respawn fences it. PURE — the
    rotation lands in the §2.6 candidate inside the executor's atomic transaction; this function
    must NOT write back into the live binding.
    """
    return binding["lease_epoch"] + 1


def check_fence(
    binding: dict,
    expected_owner_token: str | None,
    expected_lease_epoch: int | None,
) -> bool:
    """Predicate: do the presented credentials still own the LIVE binding? (§2.5 / §8 / §2.6).

    Returns ``True`` iff every PRESENTED credential matches the live binding, and ``False`` if any
    presented credential is stale/mismatched. A pure predicate — it NEVER mutates the binding
    (non-destructive de-authorization: a rejected check leaves ownership intact; the caller journals
    ``stale_return_ignored``).

    Semantics (mirrors the §2.6 executor precondition, which guards each credential with
    ``is not None``):

      * ``expected_owner_token`` is None  -> the token precondition is NOT applied (no credential
        presented on that axis). It is the un-fenced internal-mutator path, not a stale rejection.
      * ``expected_owner_token`` is not None -> it must EQUAL the live ``owner_token``, else False.
        Because the token embeds the epoch, this rejects a lower-epoch (stale), a higher-epoch
        (forged-future), and a different-session (different-incarnation) token alike — only the
        token matching the LIVE binding owns it.
      * ``expected_lease_epoch`` is None  -> the epoch precondition is NOT applied.
      * ``expected_lease_epoch`` is not None -> it must EQUAL the live ``lease_epoch``, else False.
        The epoch credential is independently load-bearing: a presented stale epoch is rejected even
        when the token argument is None.

    None/None (nothing presented) therefore passes -> True: the absence of a credential is not a
    stale-credential rejection.
    """
    if expected_owner_token is not None and expected_owner_token != binding["owner_token"]:
        return False
    if expected_lease_epoch is not None and expected_lease_epoch != binding["lease_epoch"]:
        return False
    return True
