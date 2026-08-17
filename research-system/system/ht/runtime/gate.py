"""One closed capability gate shared by every ordinary runtime surface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ht.errors import HtError

from .capability import (
    B1_TOP_NAMES,
    CapabilityState,
    inspect_capability_state,
)
from .replay import ReplayState, upgrade_context_from_capability
from .state import UpgradeContext


ROLE_INIT_REQUIRED = "role-init-required"
ROLE_RUNTIME_UPGRADED = "role-runtime-upgraded"


@dataclass(frozen=True)
class RuntimeGate:
    """Typed physical capability state and its sole replay interpretation."""

    capability: CapabilityState
    upgrade: UpgradeContext | None


def inspect_runtime_gate(
    estate: Path,
    runtime_id: str,
    *,
    allow_live_b1_operation_temps: bool = False,
) -> RuntimeGate:
    """Classify the marker before ordinary inventory/replay can select a branch."""

    capability = inspect_capability_state(
        estate,
        runtime_id=runtime_id,
        base_top_names=B1_TOP_NAMES,
        allow_live_b1_operation_temps=allow_live_b1_operation_temps,
    )
    if capability.branch == "repair-prefix":
        raise HtError(
            f"{ROLE_INIT_REQUIRED}: interrupted role activation must be repaired by "
            "ht role init --json (B2 §3.1)"
        )
    if capability.branch == "unupgraded":
        return RuntimeGate(capability, None)
    if capability.branch == "upgraded-complete":
        assert capability.capability is not None
        return RuntimeGate(
            capability,
            upgrade_context_from_capability(capability.capability),
        )
    raise HtError(f"unknown runtime capability branch {capability.branch!r} (B2 §3.1)")


def require_b1_submission_allowed(state: ReplayState) -> None:
    """Reject new synthetic B1 ownership before request publication or WAL use."""

    if state.upgrade is not None:
        raise HtError(
            f"{ROLE_RUNTIME_UPGRADED}: synthetic runtime submission is disabled after "
            "role activation (B2 §16)"
        )


def is_role_init_required(error: BaseException) -> bool:
    return isinstance(error, HtError) and error.message.startswith(
        f"{ROLE_INIT_REQUIRED}:"
    )
