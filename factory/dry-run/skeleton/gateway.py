"""payments/gateway stub — Stripe charge adapter (contracts/payments-charge.md).

THROWAWAY SPIKE. Fake Stripe. Implements ChargeService.charge() -> ChargeResult
(three outcomes per ADR-007). Derives a Stripe Idempotency-Key from the domain key
(ADR-009 / CI-2) and dedups on it (CI-1).

KEY SPIKE QUESTION exercised here: ChargeResult is SYNCHRONOUS, but ADR-005 says
the authoritative outcome arrives async via webhook. So what does a CHARGED that
is "pending at Stripe" look like? The provisional contract has no PENDING/
AUTH_PENDING outcome. This stub is forced to either (a) lie and return CHARGED
synchronously, or (b) invent an outcome the contract does not define. See finding.
"""

from dataclasses import dataclass
from substrate import Money, ChargeId, PaymentIntentId, IdempotencyKey


# ---- ChargeRequest / ChargeResult (contracts/payments-charge.md) ------------

@dataclass
class ChargeRequest:
    order_id: str
    amount: Money
    domain_idempotency_key: IdempotencyKey
    payment_token: str  # tokenized ref, never PAN (ADR-011)


# Three+1 outcomes. The contract lists CHARGED / ALREADY_CHARGED /
# REFUSED_FOR_SAFETY / FAILED. None of them expresses "accepted, confirmation
# pending" — the async reality of Stripe PaymentIntents.
@dataclass
class Charged:
    charge_id: ChargeId
    amount: Money
    intent_id: PaymentIntentId  # NOT in the provisional contract — added by skeleton

@dataclass
class AlreadyCharged:
    charge_id: ChargeId

@dataclass
class RefusedForSafety:
    reason: str

@dataclass
class Failed:
    reason: str
    retriable: bool


def stripe_idem_key(domain_key: IdempotencyKey) -> str:
    """CI-2 / ADR-009: deterministic derivation. Skeleton: prefix, not random."""
    return "stripe_" + domain_key


class FakeStripe:
    """Stripe sandbox stand-in. PaymentIntent semantics: create returns a pending
    intent; confirmation comes later via a webhook the caller must wait for."""
    def __init__(self):
        self._intents_by_idem: dict[str, str] = {}  # stripe idem key -> intent id
        self._counter = 0
        self.webhooks_to_deliver = []

    def create_payment_intent(self, amount: Money, token: str, idem_key: str):
        if idem_key in self._intents_by_idem:
            # Stripe dedup: same idempotency key -> same intent, no new charge
            return self._intents_by_idem[idem_key], False  # (intent_id, created_new)
        self._counter += 1
        intent_id = f"pi_{self._counter}"
        self._intents_by_idem[idem_key] = intent_id
        # PaymentIntent is created in 'requires_confirmation'/'processing'; the
        # 'succeeded' truth is delivered async:
        self.webhooks_to_deliver.append({
            "type": "payment_intent.succeeded",
            "event_id": f"evt_{self._counter}",
            "intent_id": intent_id,
            "amount": amount,
        })
        return intent_id, True


class StripeChargeAdapter:
    """Implements ChargeService. Dedups on derived stripe key (CI-1)."""
    def __init__(self, stripe: FakeStripe):
        self.stripe = stripe
        self._charge_by_domain_key: dict[IdempotencyKey, ChargeId] = {}
        self._charge_counter = 0

    def charge(self, req: ChargeRequest):
        # CI-1: same domain key -> at most one CHARGED.
        if req.domain_idempotency_key in self._charge_by_domain_key:
            return AlreadyCharged(self._charge_by_domain_key[req.domain_idempotency_key])

        sk = stripe_idem_key(req.domain_idempotency_key)
        intent_id, created_new = self.stripe.create_payment_intent(
            req.amount, req.payment_token, sk
        )
        if not created_new:
            # Stripe deduped but we have no local record -> ambiguous.
            # CI-4 / C-G2: refuse rather than risk a second charge.
            return RefusedForSafety("stripe deduped intent we have no record of")

        self._charge_counter += 1
        charge_id = f"ch_{self._charge_counter}"
        self._charge_by_domain_key[req.domain_idempotency_key] = charge_id
        # PROBLEM: the charge is NOT yet confirmed (async). But the only positive
        # outcome the contract gives us is CHARGED. We return CHARGED here, which
        # the contract docstring says means "a new charge was created now" —
        # arguably true for the *intent*, but it is NOT yet money-taken-confirmed.
        return Charged(charge_id, req.amount, intent_id)
