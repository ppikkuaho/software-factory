"""necro — the basic necro / resume delta-brief assembly seam (IMPLEMENTATION-PLAN §2.12).

THE single load-bearing property: ``resume_brief`` DELEGATES the gate-firewall to the SINGLE
enforcement point in ``chokepoint.resume`` — it CALLS ``chokepoint.resume`` and NEVER re-implements
the gate check / the refusal. There is exactly ONE place that can issue a ``--resume``
(``chokepoint.resume``), so the never-resume-across-the-gate firewall cannot be bypassed via this
seam (no second copy to drift).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.11 / §2.12 (necro.py: resume_brief "DELEGATES the gate-firewall to the
    SINGLE point in chokepoint.resume; it calls chokepoint.resume, never re-implements the raise /
    the gate check"); §3 module table (necro.py row); DAEMON §6.4 (resume = spawn variant through the
    same chokepoint, WITH the LOCKED gate firewall carve-out — owned by the chokepoint, not here).

BUILDER DECISIONS (the §2.12 details the frozen tests leave open — stated in the build report):

  * SIGNATURE — §2.12 names ``resume_brief(node_address) -> tuple[ResumeArgs, DeltaBrief]``. The
    frozen test calls it minimally with just ``node_address`` first, then (on a TypeError) threads
    the resume context (expected_state / expected_generation / expected_owner_token / delta_inputs /
    level_config). We accept BOTH: ``node_address`` is required; the resume-context fields are
    OPTIONAL keywords sourced from the LIVE binding when not passed (so a bare call still assembles a
    well-formed resume). FORK-NECRO-SIG: the optional-context spelling keeps the §2.12 one-arg call
    working while remaining a faithful resume seam.

  * NO SECOND FIREWALL — this module owns NO gate check. It does not read or branch on the
    gate-crossing flag and never issues a refusal of its own; it hands the whole resume (including the
    firewall decision) to ``chokepoint.resume``. That single delegation IS the enforcement.
"""

from __future__ import annotations

from typing import Optional

from harnessd import ledger
from harnessd.spawn import chokepoint


def resume_brief(
    node_address: str,
    *,
    expected_state: Optional[str] = None,
    expected_generation: Optional[int] = None,
    expected_owner_token: Optional[str] = None,
    delta_inputs: Optional[dict] = None,
    level_config=None,
):
    """Assemble the resume and DELEGATE to ``chokepoint.resume`` — the SINGLE gate-firewall point.

    The firewall is NOT re-checked here. necro hands the entire resume — including the
    never-resume-across-the-gate decision and the ``--resume`` argv construction — to
    ``chokepoint.resume``, the ONLY path that can issue a ``--resume``. So the firewall is enforced in
    EXACTLY ONE place; there is no second copy in this module to drift out of sync.

    Sources any resume-context field not explicitly passed from the LIVE binding (so the §2.12 bare
    ``resume_brief(node_address)`` call assembles a well-formed resume), then delegates. Returns
    whatever ``chokepoint.resume`` returns (the resume outcome).
    """
    # Read the live binding to source any resume-context the caller omitted. Tolerate an
    # unconfigured/absent ledger (a bare delegation call need not have a bound RUNTIME_ROOT): the
    # context simply stays None and chokepoint.resume — the single authority — validates it.
    try:
        live = ledger.read_binding(node_address) or {}
    except RuntimeError:
        live = {}

    # Source the resume context from the live binding where the caller did not supply it. (None stays
    # None when there is no binding — chokepoint.resume is the authority that validates the context.)
    state = expected_state if expected_state is not None else live.get("state")
    generation = expected_generation if expected_generation is not None else live.get("generation")
    owner_token = (
        expected_owner_token if expected_owner_token is not None else live.get("owner_token")
    )

    # DELEGATE: the single enforcement point owns the firewall + the --resume argv. necro never builds
    # a --resume itself and never branches on the crossing flag — it calls the one check.
    return chokepoint.resume(
        node_address,
        expected_state=state,
        expected_generation=generation,
        expected_owner_token=owner_token,
        delta_inputs=delta_inputs or {},
        level_config=level_config,
    )
