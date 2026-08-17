"""`ht phase set <mode>` — set the Tier-1 PC authority phase."""

from __future__ import annotations

from .. import jsonio
from ..errors import HtUsageError
from ..pipeline import DocWrite, Plan
from ._common import Ctx, today


def set_mode(ctx: Ctx, mode: str) -> Plan:
    """Build the user-authored sign-off/autonomy phase update."""
    if mode not in ("sign-off", "autonomy"):
        raise HtUsageError(f"unknown phase mode '{mode}' (sign-off|autonomy)")

    path = ctx.root.phase_json
    old = jsonio.load(path) if path.exists() else None
    new = {"mode": mode, "set_by": ctx.role, "date": today()}
    return Plan(
        role=ctx.role,
        message=f"ht phase set: {mode}",
        writes=[DocWrite(path, "phase", old, new)],
    )
