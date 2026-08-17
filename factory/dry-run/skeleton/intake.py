"""payments/intake stub — submit boundary + request idempotency.

THROWAWAY SPIKE. Implements the submit path: claim request idem key (R-007.1),
record attempt intent BEFORE charging (C-I3 / ADR-012.4), call ChargeService,
then... drive the order transition? OR leave it to webhooks? THIS is the
unresolved RENEGOTIATION FLAG (ADR-005). The skeleton is forced to pick, and the
choice it is forced into is itself the finding.
"""

from dataclasses import dataclass
from substrate import IdempotencyStore, EventLog, DomainEvent, FRESH, Duplicate, Clock
from gateway import ChargeRequest, Charged, AlreadyCharged, RefusedForSafety, Failed
from orders import OrderPaymentState, TRANSITIONED, ALREADY_PAID


@dataclass
class SubmitRequest:
    order_id: str
    amount: object  # Money
    idempotency_key: str
    payment_token: str


@dataclass
class SubmitResult:
    status: str   # ACCEPTED_PENDING | ALREADY_ACCEPTED | REFUSED | FAILED
    detail: str


class SubmitHandler:
    def __init__(self, idem: IdempotencyStore, events: EventLog,
                 charge_service, orders: OrderPaymentState, clock: Clock):
        self.idem = idem
        self.events = events
        self.charge_service = charge_service
        self.orders = orders
        self.clock = clock

    def submit(self, req: SubmitRequest) -> SubmitResult:
        # C-I1 / R-007.1: claim the request idempotency key.
        claim = self.idem.claim(req.idempotency_key)
        if isinstance(claim, Duplicate):
            return SubmitResult("ALREADY_ACCEPTED", "request idem key replay")

        # C-I3 / ADR-012.4: durably record attempt intent BEFORE charging.
        self.events.append(DomainEvent(
            "charge-attempted",
            {"order_id": req.order_id, "key": req.idempotency_key},
            self.clock.now(),
        ))

        result = self.charge_service.charge(ChargeRequest(
            order_id=req.order_id,
            amount=req.amount,
            domain_idempotency_key=req.idempotency_key,
            payment_token=req.payment_token,
        ))

        if isinstance(result, Charged):
            self.events.append(DomainEvent(
                "charge-created",
                {"charge_id": result.charge_id, "intent_id": result.intent_id},
                self.clock.now(),
            ))
            self.idem.commit(req.idempotency_key, result.charge_id)
            # === RENEGOTIATION FORK ===
            # Per ADR-005 the order->PAID transition is driven by the AUTHORITATIVE
            # webhook channel, NOT here. So intake must NOT mark_paid. It returns
            # "accepted, pending confirmation". The contract's SubmitResult has no
            # such status in any provisional doc; we invent ACCEPTED_PENDING.
            return SubmitResult("ACCEPTED_PENDING", result.charge_id)

        if isinstance(result, AlreadyCharged):
            self.idem.commit(req.idempotency_key, result.charge_id)
            return SubmitResult("ALREADY_ACCEPTED", result.charge_id)

        if isinstance(result, RefusedForSafety):
            # C-I4 / T-3: do not commit a success; safe-reject.
            self.idem.commit(req.idempotency_key, "REFUSED")
            return SubmitResult("REFUSED", result.reason)

        if isinstance(result, Failed):
            self.idem.commit(req.idempotency_key, "FAILED")
            self.orders.mark_failed(req.order_id, result.reason)
            return SubmitResult("FAILED", result.reason)

        raise AssertionError("unknown ChargeResult variant")
