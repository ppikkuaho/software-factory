"""Increment 10 — FROZEN acceptance for necro.py (the delta-brief assembly seam). Tests ONLY —
NO implementation. RED first.

THE load-bearing property: necro.resume_brief DELEGATES the gate-firewall to the SINGLE
enforcement point in chokepoint.resume — it CALLS chokepoint.resume and NEVER re-implements the
gate check / the raise. There is exactly ONE place that can issue a --resume (chokepoint.resume),
so the never-resume-across-the-gate firewall cannot be bypassed via the necro seam.

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.11 — necro.py: resume_brief "DELEGATES the gate-firewall to the SINGLE
    point in chokepoint.resume (it calls chokepoint.resume, never re-implements the raise / the
    gate check)."
  * IMPLEMENTATION-PLAN §2.12 — resume_brief(node_address): "Assembles the delta brief. The
    gate-firewall is NOT re-checked here — it delegates to the SINGLE enforcement point
    (chokepoint.resume). The --resume argv is constructed ONLY after that check passes, so a resume
    across the gate is structurally impossible (not merely guarded twice)."
  * IMPLEMENTATION-PLAN §3 module table (necro.py row): "Does NOT own a second copy of the
    gate-firewall — the NEVER-RESUME-ACROSS-THE-GATE check lives in EXACTLY ONE place
    (chokepoint.resume); necro.resume_brief calls that single check rather than re-implementing a
    raise."
  * DAEMON §6.4 — resume / necro is a spawn variant through the same chokepoint, WITH the gate
    firewall as the LOCKED carve-out.

NO IMPLEMENTATION here — harnessd/necro.py does not exist yet (RED until written).

Load-bearing properties (each pins a mutant):
  * resume_brief routes through chokepoint.resume — the single firewall point (mutant: necro
    re-implements its own gate check / its own --resume path -> a SECOND enforcement point exists,
    bypassable -> caught: we assert chokepoint.resume is the one called).
  * resume_brief does NOT re-raise / re-check the gate itself (mutant: a duplicate raise in necro
    -> two firewalls drift -> caught: the source carries no second gate check, and the delegation
    is what enforces it).
"""

from __future__ import annotations

import importlib
import inspect

import pytest


def _necro():
    return importlib.import_module("harnessd.necro")


def _chokepoint():
    return importlib.import_module("harnessd.spawn.chokepoint")


# ===========================================================================
# resume_brief DELEGATES to chokepoint.resume — the single firewall point.
#
# We patch chokepoint.resume with a sentinel and assert necro.resume_brief CALLS it. The gate
# firewall lives in EXACTLY ONE place; necro must route through it, not re-implement it.
#
# Mutant killed: necro.resume_brief builds its own --resume / runs its own gate check instead of
# calling chokepoint.resume -> the sentinel is never called -> caught.
# ===========================================================================

def test_resume_brief_delegates_to_chokepoint_resume(monkeypatch):
    necro = _necro()
    chokepoint = _chokepoint()

    calls = {"n": 0, "args": None, "kwargs": None}

    def sentinel_resume(*args, **kwargs):
        calls["n"] += 1
        calls["args"] = args
        calls["kwargs"] = kwargs
        # Return a benign object so resume_brief can proceed.
        return object()

    monkeypatch.setattr(chokepoint, "resume", sentinel_resume)
    # If necro imported `resume` by name, patch its local reference too (so the delegation is
    # caught regardless of import style).
    if hasattr(necro, "resume"):
        monkeypatch.setattr(necro, "resume", sentinel_resume, raising=False)
    if hasattr(necro, "chokepoint"):
        monkeypatch.setattr(necro.chokepoint, "resume", sentinel_resume, raising=False)

    # Call the seam. We pass a node_address (the §2.12 signature) and tolerate extra kwargs the
    # implementation may thread (delta_inputs / level_config) by trying the minimal call first.
    try:
        necro.resume_brief("proj/widget#exec")
    except TypeError:
        # The implementation may require the resume context; thread plausible kwargs.
        necro.resume_brief(
            "proj/widget#exec",
            expected_state="running",
            expected_generation=0,
            expected_owner_token=None,
            delta_inputs={},
            level_config=None,
        )

    assert calls["n"] >= 1, (
        "necro.resume_brief must DELEGATE to chokepoint.resume — the SINGLE gate-firewall "
        "enforcement point (mutant: necro builds its own --resume / re-checks the gate -> the "
        "sentinel is never called -> a second, bypassable firewall exists)"
    )


def test_necro_does_not_reimplement_the_gate_check(monkeypatch):
    """necro.py must NOT carry a SECOND copy of the gate-firewall: the never-resume-across-the-gate
    check lives in EXACTLY ONE place (chokepoint.resume). A duplicated check in necro is two
    firewalls that can drift.

    Mutant killed: a duplicate gate-crossed raise inside necro.resume_brief -> caught (the necro
    source must not implement its own gate_crossed_at refusal; it delegates).
    """
    necro = _necro()
    source = inspect.getsource(necro)

    # The gate-firewall raise/refusal is NOT re-implemented here. necro may MENTION the delegation
    # in a comment, but it must not contain its own gate_crossed_at-conditional refusal logic.
    # Heuristic: necro must not both reference gate_crossed_at AND raise/refuse on it locally.
    lowered = source.lower()
    has_local_gate_branch = (
        "gate_crossed_at" in lowered
        and ("if " in lowered)
        and ("raise" in lowered or "refuse" in lowered)
    )
    assert not has_local_gate_branch, (
        "necro.py must NOT re-implement the gate-firewall (a local gate_crossed_at refusal/raise): "
        "the never-resume-across-the-gate check lives in EXACTLY ONE place (chokepoint.resume); "
        "necro delegates to it"
    )
