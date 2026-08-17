"""Acceptance-test implementation for the payments exactly-once slice.

The module intentionally keeps the seam small: an in-memory payment system with
request-key replay, purchase-key unique claims, webhook event deduplication, and
a deterministic Stripe gateway double.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import threading
from typing import Dict, Optional, Tuple


Amount = Tuple[int, str]


@dataclass
class Result:
    tag: str
    intent_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ChargeRecord:
    order_id: str
    amount: Amount
    idem_key: str
    purchase_key: str
    state: str = "PENDING"
    intent_id: Optional[str] = None

    @property
    def tag(self) -> str:
        return self.state


@dataclass
class StripeIntent:
    intent_id: str
    status: str


class AmbiguousGatewayOutcome(Exception):
    pass


@dataclass
class RequestMemo:
    tag: str = "IN_PROGRESS"
    intent_id: Optional[str] = None
    reason: Optional[str] = None
    _done: threading.Event = field(default_factory=threading.Event)

    def finish(
        self,
        tag: str,
        *,
        intent_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.tag = tag
        self.intent_id = intent_id
        self.reason = reason
        self._done.set()

    def as_replay_result(self) -> Result:
        self._done.wait()
        if self.tag == "ACCEPTED_PENDING":
            return Result("ALREADY_ACCEPTED", intent_id=self.intent_id)
        return Result(self.tag, intent_id=self.intent_id, reason=self.reason)


class AtomicIdempotencyStore:
    """Thread-safe unique-claim store for req:, purchase:, and evt: keys."""

    is_atomic_concurrent = True

    def __init__(self) -> None:
        self._claims: Dict[str, object] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, value: object) -> Tuple[bool, object]:
        with self._lock:
            if key in self._claims:
                return False, self._claims[key]
            self._claims[key] = value
            return True, value

    def get(self, key: str) -> Optional[object]:
        with self._lock:
            return self._claims.get(key)


class FakeStripeGateway:
    def __init__(self) -> None:
        self.seen_tokens = []
        self._intents_by_idem_key: Dict[str, StripeIntent] = {}
        self._next_intent_status = "succeeded"
        self._ambiguous = False
        self._counter = 0
        self._lock = threading.Lock()

    def set_next_intent_status(self, status: str) -> None:
        with self._lock:
            self._next_intent_status = status

    def set_ambiguous(self, value: bool) -> None:
        with self._lock:
            self._ambiguous = value

    def create_payment_intent(
        self,
        *,
        amount: Amount,
        token: str,
        stripe_idempotency_key: str,
    ) -> StripeIntent:
        del amount
        with self._lock:
            self.seen_tokens.append(token)
            if self._ambiguous:
                raise AmbiguousGatewayOutcome()

            existing = self._intents_by_idem_key.get(stripe_idempotency_key)
            if existing is not None:
                return existing

            self._counter += 1
            intent = StripeIntent(f"pi_{self._counter}", self._next_intent_status)
            self._next_intent_status = "succeeded"
            self._intents_by_idem_key[stripe_idempotency_key] = intent
            return intent

    def intent_count(self) -> int:
        with self._lock:
            return len(self._intents_by_idem_key)


class ChargeRecordStore:
    def __init__(self) -> None:
        self._records = []
        self._by_intent: Dict[str, ChargeRecord] = {}
        self._last_intent_by_order: Dict[str, str] = {}
        self._lock = threading.Lock()

    def create_pending(
        self,
        *,
        order_id: str,
        amount: Amount,
        idem_key: str,
        purchase_key: str,
    ) -> ChargeRecord:
        record = ChargeRecord(
            order_id=order_id,
            amount=amount,
            idem_key=idem_key,
            purchase_key=purchase_key,
        )
        with self._lock:
            self._records.append(record)
        return record

    def attach_intent(self, record: ChargeRecord, intent_id: str) -> None:
        with self._lock:
            record.intent_id = intent_id
            self._by_intent[intent_id] = record
            self._last_intent_by_order[record.order_id] = intent_id

    def lookup_by_intent(self, intent_id: str) -> Optional[ChargeRecord]:
        with self._lock:
            return self._by_intent.get(intent_id)

    def mark_failed(self, record: ChargeRecord) -> None:
        with self._lock:
            record.state = "FAILED"

    def mark_refused(self, record: ChargeRecord) -> None:
        with self._lock:
            record.state = "REFUSED"

    def mark_confirmed(self, record: ChargeRecord) -> None:
        with self._lock:
            record.state = "CONFIRMED"

    def last_intent_id(self, order_id: str) -> Optional[str]:
        with self._lock:
            return self._last_intent_by_order.get(order_id)


class OrderStore:
    def __init__(self) -> None:
        self._states: Dict[str, str] = {}
        self._lock = threading.Lock()

    def state(self, order_id: str) -> str:
        with self._lock:
            return self._states.get(order_id, "READY")

    def mark_failed(self, order_id: str) -> None:
        with self._lock:
            self._states[order_id] = "FAILED"

    def mark_paid(self, order_id: str) -> None:
        with self._lock:
            self._states[order_id] = "PAID"


class PaymentsSystem:
    def __init__(self, store: AtomicIdempotencyStore) -> None:
        self.store = store
        self.stripe = FakeStripeGateway()
        self.charge_records = ChargeRecordStore()
        self._orders = OrderStore()

    def submit(self, order_id: str, amount: Amount, idem_key: str, token: str) -> Result:
        request_memo = RequestMemo()
        fresh_request, existing_request = self.store.claim(f"req:{idem_key}", request_memo)
        if not fresh_request:
            return existing_request.as_replay_result()

        purchase_key = _purchase_key(order_id, amount)
        fresh_purchase, _ = self.store.claim(f"purchase:{purchase_key}", idem_key)
        if not fresh_purchase:
            request_memo.finish("REFUSED", reason="PURCHASE_ALREADY_CLAIMED")
            return Result("REFUSED", reason="PURCHASE_ALREADY_CLAIMED")

        record = self.charge_records.create_pending(
            order_id=order_id,
            amount=amount,
            idem_key=idem_key,
            purchase_key=purchase_key,
        )
        try:
            intent = self.stripe.create_payment_intent(
                amount=amount,
                token=token,
                stripe_idempotency_key=_stripe_idempotency_key(purchase_key),
            )
        except AmbiguousGatewayOutcome:
            self.charge_records.mark_refused(record)
            request_memo.finish("REFUSED", reason="REFUSED_FOR_SAFETY")
            return Result("REFUSED", reason="REFUSED_FOR_SAFETY")

        self.charge_records.attach_intent(record, intent.intent_id)
        if intent.status == "failed":
            self.charge_records.mark_failed(record)
            self._orders.mark_failed(order_id)
            request_memo.finish(
                "FAILED",
                intent_id=intent.intent_id,
                reason="GATEWAY_FAILED",
            )
            return Result("FAILED", intent_id=intent.intent_id, reason="GATEWAY_FAILED")

        request_memo.finish("ACCEPTED_PENDING", intent_id=intent.intent_id)
        return Result("ACCEPTED_PENDING", intent_id=intent.intent_id)

    def deliver_webhook(self, raw_event, signature: str) -> Result:
        if signature != "valid":
            return Result("REJECTED_SIGNATURE")

        event_id = raw_event["id"]
        intent_id = raw_event["intent_id"]
        fresh_event, _ = self.store.claim(f"evt:{event_id}", True)
        if not fresh_event:
            return Result("DUPLICATE_NOOP")

        record = self.charge_records.lookup_by_intent(intent_id)
        if record is None:
            return Result("PARKED_ORPHAN")

        if raw_event.get("status") == "succeeded":
            self.charge_records.mark_confirmed(record)
            self._orders.mark_paid(record.order_id)
            return Result("CONFIRMED")

        return Result("DUPLICATE_NOOP")

    def order_state(self, order_id: str) -> str:
        return self._orders.state(order_id)

    def last_intent_id(self, order_id: str) -> Optional[str]:
        return self.charge_records.last_intent_id(order_id)


def build_system(**kwargs) -> PaymentsSystem:
    concurrent_store = kwargs.pop("concurrent_store", False)
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError(f"unsupported build_system kwargs: {unknown}")
    if concurrent_store is not True and concurrent_store is not False:
        raise TypeError("concurrent_store must be a boolean when provided")
    return PaymentsSystem(store=AtomicIdempotencyStore())


def _purchase_key(order_id: str, amount: Amount) -> str:
    minor_units, currency = amount
    payload = f"{order_id}\x1f{minor_units}\x1f{currency}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stripe_idempotency_key(purchase_key: str) -> str:
    return "stripe:" + hashlib.sha256(purchase_key.encode("ascii")).hexdigest()
