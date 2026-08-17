"""Walking-skeleton substrate stubs (stdlib only).

THROWAWAY SPIKE CODE. Not production. Built first per ADR-001 / build-order step 1.
Purpose: just enough of Money, IDs, EventLog, IdempotencyStore, Clock to thread
ONE charge path end-to-end and pressure-test the provisional interface contracts
in dry-run/L2/contracts/.

In-memory, single-threaded. Does NOT satisfy C-S1 (atomic claim under true
concurrency) or C-S2 (crash-safety) — those are out of scope for a skeleton that
only proves the connections hold (PROJECT-PLANNING "Walking Skeleton" section).
The skeleton's job is to validate interface SHAPE, not the hard guarantees.
"""

from dataclasses import dataclass, field
from typing import Optional, Any


# ---- Value types (substrate-ports.md "Value types") -------------------------

@dataclass(frozen=True)
class Money:
    """ADR-002: integer minor units + ISO-4217 currency. No floats."""
    minor_units: int
    currency: str

    def __post_init__(self):
        if not isinstance(self.minor_units, int):
            raise TypeError("Money.minor_units must be int (minor units)")
        if len(self.currency) != 3:
            raise ValueError("currency must be ISO-4217 3-letter")

    def same_currency(self, other: "Money") -> bool:
        return self.currency == other.currency


# Typed IDs are just newtype-ish wrappers for the skeleton.
OrderId = str
ChargeId = str
PaymentIntentId = str
IdempotencyKey = str
EventId = str


class Clock:
    """Injectable time source. Skeleton uses a monotonic counter."""
    def __init__(self):
        self._t = 0

    def now(self) -> int:
        self._t += 1
        return self._t


# ---- EventLog port (substrate-ports.md Port: EventLog) ----------------------

@dataclass
class DomainEvent:
    kind: str
    payload: dict
    at: int


class EventLog:
    """SE-1: append-only, immutable once written."""
    def __init__(self):
        self._events: list[DomainEvent] = []

    def append(self, event: DomainEvent) -> None:
        self._events.append(event)

    def read(self, stream: Optional[str] = None) -> list[DomainEvent]:
        return list(self._events)


# ---- IdempotencyStore port (substrate-ports.md Port: IdempotencyStore) ------

FRESH = "FRESH"


@dataclass
class Duplicate:
    prior_result: Any


class IdempotencyStore:
    """claim(key) -> FRESH | Duplicate(prior_result); commit(key, result).

    SI-1 atomicity is faked (single-threaded). SI-2 holds. Namespacing (request
    keys vs event-id keys) is the caller's responsibility — skeleton uses one map,
    which is ALREADY a finding (see skeleton notes): the contract says keyspaces
    are namespaced but the port signature carries no namespace argument.
    """
    def __init__(self):
        self._claimed: dict[IdempotencyKey, Optional[Any]] = {}

    def claim(self, key: IdempotencyKey):
        if key in self._claimed:
            return Duplicate(self._claimed[key])
        self._claimed[key] = None  # claimed, not yet committed
        return FRESH

    def commit(self, key: IdempotencyKey, result: Any) -> None:
        self._claimed[key] = result
