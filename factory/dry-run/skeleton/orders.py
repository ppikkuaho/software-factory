"""orders touchpoint stub (contracts/orders-touchpoint.md).

THROWAWAY SPIKE. Implements OrderPaymentState: get_state / mark_paid / mark_failed.
ADR-008: orders is the single source of truth for paid-state.
"""

from dataclasses import dataclass
from substrate import Money, OrderId, ChargeId, IdempotencyKey

READY = "READY"
PAID = "PAID"
FAILED = "FAILED"

# mark_paid outcomes
TRANSITIONED = "Transitioned"
ALREADY_PAID = "AlreadyPaid"
REJECTED = "Rejected"


@dataclass
class _Order:
    order_id: OrderId
    total: Money
    state: str = READY
    paid_charge_id: ChargeId = None
    paid_idem_key: IdempotencyKey = None


class OrderPaymentState:
    def __init__(self):
        self._orders: dict[OrderId, _Order] = {}

    def seed(self, order_id: OrderId, total: Money):
        # skeleton helper: slice assumes order arrives READY (OI-3, R-010 deferred)
        self._orders[order_id] = _Order(order_id, total)

    def get_state(self, order_id: OrderId) -> str:
        return self._orders[order_id].state

    def mark_paid(self, order_id, charge_id, amount, idempotency_key):
        o = self._orders[order_id]
        # OI-1 idempotent transition
        if o.state == PAID:
            if o.paid_idem_key == idempotency_key:
                return ALREADY_PAID
            return REJECTED  # already paid by a different attempt
        if o.state != READY:  # OI-3
            return REJECTED
        # OI-2 amount must match recorded charge
        if not amount.same_currency(o.total) or amount.minor_units != o.total.minor_units:
            return REJECTED
        o.state = PAID
        o.paid_charge_id = charge_id
        o.paid_idem_key = idempotency_key
        return TRANSITIONED

    def mark_failed(self, order_id, reason):
        o = self._orders[order_id]
        if o.state == READY:
            o.state = FAILED
