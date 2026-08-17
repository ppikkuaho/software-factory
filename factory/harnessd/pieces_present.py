"""pieces_present — the deterministic, NO-MODEL leak-catcher (Phase 6, instrument (P)).

The cheapest behavioural-validation instrument. For each spawn boundary it assembles the
runtime-NEUTRAL brief + load-manifest (via the REAL ``brief.assemble_neutral`` — never mocked) and
asserts the brief a level is handed is COMPLETE: every manifest doc PATH resolves under the harness
root (the read-allow graph), the brief carries every field the receiving level needs, and
``role_variant`` selects the CORRECT per-seat bundle. It converts a "silent gap" (a missing piece a
faithful agent would route AROUND — design/BEHAVIOURAL-VALIDATION.md §"A leak is a flow that ROUTES
AROUND a gap") into a LOUD, front-loaded failure at the cheapest possible point.

Authoritative sources (grounded, not recalled):
  * IMPLEMENTATION-PLAN Increment-18 (Phase 6): for each spawn boundary assemble the brief +
    load-manifest and assert every manifest doc PATH resolves; the brief is decision-complete for the
    receiving level (spec pointer present, frozen ``acceptance.md`` present for executor seats); the
    ``role_variant`` selects the CORRECT per-seat manifest (L5<->swe-handbook, L3<->planning-template).
    DONE-TEST: a deliberately removed/renamed role doc OR a missing brief field makes the harness FAIL.
    No model.
  * design/BEHAVIOURAL-VALIDATION.md — the "(P) Pieces-present" row: catches "a missing field / a
    dangling ref / a system-it-leverages not in place in the brief+load-manifest a level is handed".
  * design/ROLE-RESOLUTION.md §2 — the per-seat LOAD-MANIFEST tiers (Shared-always / Per-level /
    Node-local incl. the frozen read-only ``acceptance.md``); seat-variants select different
    manifests (``#exec`` / ``#review`` / ``#test``).
  * design/ROLE-RESOLUTION.md §3 — read-in-place against the harness root (the read-allow graph):
    every manifest doc is a path relative to the harness root the node reads in place.
  * harnessd/spawn/brief.py — ``assemble_neutral(node_address, level_config, work_node)`` ->
    ``NeutralContract`` with ``load_manifest`` (ordered role-doc PATHS), ``spec_pointer``,
    ``frozen_acceptance_ref``, ``node_address``, ``level``, ``role_variant``.
  * harnessd/config.py — ``LevelConfig`` per level; ``role_variant`` is the per-seat selector
    (L5 = ``L5#exec``).

BIAS TO REAL: the checker runs against the REAL operational/ corpus on disk via the REAL
``brief.assemble_neutral`` — the brief and the filesystem are never mocked. The harness root is the
ONE resolution seat the catch-tests repoint at a deliberately-broken temp copy (``harness_root()``),
so a removed/renamed doc can be detected without touching the real tree (ROLE-RESOLUTION §3).

BUILDER DECISIONS (stated in the build report — the frozen test pins BEHAVIOUR, not a signature):

  * ENTRY POINT — ``check_boundary(node_address, level_config, work_node) -> PiecesResult``. The
    result is a small frozen dataclass carrying ``ok`` + a ``fail_message`` that NAMES the specific
    missing piece (the text the catch-tests assert on). It also normalizes to ``bool(result)`` and is
    iterable as ``(ok, message)`` so a tolerant caller can consume it either way.

  * HARNESS_ROOT — resolved by ``harness_root()`` (default: the repo root, ``parents[1]`` of this
    module). This is the single seat the catch-tests monkeypatch at a temp corpus. It is the read-allow
    graph root the relative manifest paths resolve against (ROLE-RESOLUTION §3). FORK-ROOT: the
    default is the on-disk repo root (the daemon's launchd CWD is not guaranteed to be the repo root —
    config.py's resolution note — but the deterministic checker resolves against the committed tree).

  * EXECUTOR-SEAT determination (who must carry ``acceptance.md``) — keyed off ``role_variant`` (the
    per-seat selector, ROLE-RESOLUTION §4): the bare level token ``L5`` OR a ``#exec`` / ``#test``
    suffix is an executor seat. This matches the frozen test's ``_is_executor_seat`` and the design's
    "executor seats (L5 / #exec / #test) MUST have the frozen acceptance.md".

  * PER-SEAT BUNDLE correctness — derived from the TRUE seat identity (``level_config.level``), NOT
    from ``role_variant``. ROLE-RESOLUTION §4: ``level`` is the seat; ``role_variant`` is the selector
    that is SUPPOSED to match it. A seat whose ``role_variant`` selects another level's manifest (the
    wrong-bundle leak: an L5 seat handed the L3 planning-template / missing the L5 swe-handbook) is
    caught by checking the assembled manifest against the TRUE seat's required+forbidden docs. This is
    the load-bearing per-seat-selection check (a wrong bundle is a leak — BEHAVIOURAL-VALIDATION).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from harnessd import config
from harnessd.spawn import brief as brief_mod


# ---------------------------------------------------------------------------
# HARNESS_ROOT — the read-allow graph root the relative manifest paths resolve against
# (ROLE-RESOLUTION §3). The ONE resolution seat the catch-tests repoint at a temp corpus.
# ---------------------------------------------------------------------------

def harness_root() -> Path:
    """Resolve the harness root the relative manifest paths read in place against (§3).

    Default: the repo root — this module lives at ``<root>/harnessd/pieces_present.py``, so the root
    is ``parents[1]``. The catch-tests monkeypatch this to point the deterministic check at a
    deliberately-broken temp copy of the corpus without touching the real tree.
    """
    return Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Executor-seat determination (who must carry the frozen acceptance.md — ROLE-RESOLUTION §2 node-local
# tier). Keyed off ``role_variant`` (the per-seat selector) so it matches the design + the frozen test.
# ---------------------------------------------------------------------------

_EXECUTOR_TOKENS: tuple[str, ...] = ("L5",)
_EXECUTOR_SEAT_SUFFIXES: tuple[str, ...] = ("#exec", "#test")


def _is_executor_seat(role_variant: str) -> bool:
    """True if ``role_variant`` is an EXECUTOR seat (must carry the frozen acceptance.md ref).

    The bare level token ``L5`` OR a ``#exec`` / ``#test`` seat suffix. Mirrors the design's
    "executor seats (L5 / #exec / #test)" and the frozen test's helper.
    """
    role_variant = role_variant or ""
    token = role_variant.split("#", 1)[0]
    if token in _EXECUTOR_TOKENS:
        return True
    return any(role_variant.endswith(sfx) or sfx in role_variant for sfx in _EXECUTOR_SEAT_SUFFIXES)


# ---------------------------------------------------------------------------
# Per-seat bundle expectations (ROLE-RESOLUTION §2 "Per-level" extras), keyed by the TRUE seat
# (``level_config.level``). REQUIRED: the per-seat doc the manifest MUST carry; FORBIDDEN: another
# seat's per-seat doc that the manifest MUST NOT carry (a wrong bundle is a leak). These mirror the
# tiers brief.py assembles, so a manifest that does NOT match its declared seat is caught here.
# ---------------------------------------------------------------------------

_SEAT_REQUIRED: dict[str, tuple[str, ...]] = {
    "L1": ("operational/L1/handbook.md", "operational/L1/intake-session-template.md"),
    "L3": ("operational/L3/planning-template.md",),
    "L5": ("operational/L5/swe-handbook.md",),
}

# The distinguishing per-seat docs (every other seat must NOT carry these). Derived as: for a seat,
# the union of every OTHER seat's required docs, minus its own.
_DISTINGUISHING_DOCS: tuple[str, ...] = tuple(
    sorted({doc for docs in _SEAT_REQUIRED.values() for doc in docs})
)


def _forbidden_for(level: str) -> tuple[str, ...]:
    """The per-seat docs a given seat must NOT carry (every other seat's distinguishing docs)."""
    own = set(_SEAT_REQUIRED.get(level, ()))
    return tuple(doc for doc in _DISTINGUISHING_DOCS if doc not in own)


# ---------------------------------------------------------------------------
# The check result — a small frozen dataclass that NAMES the missing piece.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PiecesResult:
    """The per-boundary pieces-present verdict.

    ``ok`` is the pass/fail; ``fail_message`` NAMES the specific missing piece (the text the
    catch-tests assert on) — never a silent pass / a generic error. Normalizes to ``bool(result)``
    and iterates as ``(ok, message)`` so a tolerant caller can consume either shape.
    """

    ok: bool
    boundary: str
    fail_message: str = ""

    @property
    def message(self) -> str:
        return self.fail_message

    @property
    def reason(self) -> str:
        return self.fail_message

    def __bool__(self) -> bool:
        return self.ok

    def __iter__(self) -> Iterator:
        yield self.ok
        yield self.fail_message


def _fail(boundary: str, message: str) -> PiecesResult:
    return PiecesResult(ok=False, boundary=boundary, fail_message=message)


def _ok(boundary: str) -> PiecesResult:
    return PiecesResult(ok=True, boundary=boundary, fail_message="")


# ---------------------------------------------------------------------------
# check_boundary — the per-boundary check (the frozen test drives this through a tolerant adapter).
# ---------------------------------------------------------------------------

def check_boundary(node_address: str, level_config, work_node: Optional[dict]) -> PiecesResult:
    """Assert the brief+load-manifest handed across ONE spawn boundary is COMPLETE (Inc-18).

    Assembles the REAL runtime-neutral contract via ``brief.assemble_neutral`` and, in order, FAILS
    LOUD (naming the specific missing piece) if any of:

      1. the load-manifest is empty;
      2. a manifest doc PATH does not resolve under ``harness_root()`` (a removed/renamed doc — the
         read-allow graph completeness check, ROLE-RESOLUTION §3);
      3. the brief is not decision-complete for the receiving level (no node identity/address; no
         ``spec_pointer``; or — for an EXECUTOR seat — no frozen ``acceptance.md`` reference);
      4. ``role_variant`` selected the WRONG per-seat bundle (the manifest is missing the TRUE seat's
         per-seat doc, or carries another seat's distinguishing doc — a wrong bundle is a leak).

    Returns a ``PiecesResult`` whose ``fail_message`` NAMES the missing piece. Deterministic, no model.
    """
    root = harness_root()
    boundary = node_address

    contract = brief_mod.assemble_neutral(node_address, level_config, work_node or {})

    # The TRUE seat identity (the level this seat is meant to BE) and the per-seat selector that is
    # supposed to match it (ROLE-RESOLUTION §4: level is the seat, role_variant the selector).
    seat_level = (getattr(level_config, "level", None) or contract.level or "").split("#", 1)[0]
    role_variant = contract.role_variant or seat_level

    # --- 1. The manifest must be non-empty (a seat with no role docs is a missing piece) ---
    if not contract.load_manifest:
        return _fail(boundary, f"{boundary}: the load-manifest is EMPTY — no role docs selected")

    # --- 2. Every manifest doc PATH resolves under the harness root (the read-allow graph) ---
    missing = [doc for doc in contract.load_manifest if not (root / doc.split("#", 1)[0]).is_file()]
    if missing:
        return _fail(
            boundary,
            f"{boundary}: load-manifest doc(s) do NOT resolve under the harness root "
            f"(a removed/renamed role doc is a missing piece — ROLE-RESOLUTION §3 read-allow graph): "
            f"{missing}",
        )

    # --- 3. The brief is decision-complete for the receiving level ---
    if not contract.node_address:
        return _fail(boundary, f"{boundary}: the brief is missing the node identity/address")
    if not contract.spec_pointer:
        return _fail(
            boundary,
            f"{boundary}: the brief is NOT decision-complete — missing the required spec_pointer "
            f"(the receiving level has no spec to realize — ROLE-RESOLUTION §2; Inc-18)",
        )
    if _is_executor_seat(role_variant) and not contract.frozen_acceptance_ref:
        return _fail(
            boundary,
            f"{boundary}: an EXECUTOR seat ({role_variant}) is missing the frozen acceptance.md "
            f"reference — the executor's spec-anchored target (ROLE-RESOLUTION §2 node-local tier)",
        )

    # --- 4. role_variant selected the CORRECT per-seat bundle for the TRUE seat ---
    bundle_result = _check_per_seat_bundle(boundary, seat_level, role_variant, contract)
    if not bundle_result.ok:
        return bundle_result

    return _ok(boundary)


def _check_per_seat_bundle(boundary, seat_level, role_variant, contract) -> PiecesResult:
    """Assert the assembled manifest is the CORRECT per-seat bundle for the TRUE seat (ROLE-RESOLUTION §2/§4).

    The manifest must carry the TRUE seat's per-seat doc(s) and must NOT carry another seat's
    distinguishing doc. A seat whose ``role_variant`` selected another level's manifest (an L5 seat
    handed the L3 planning-template / missing the L5 swe-handbook) is the wrong-bundle leak this catches.
    """
    manifest = set(contract.load_manifest)

    # The TRUE seat's required per-seat doc(s) must be present.
    required = _SEAT_REQUIRED.get(seat_level, ())
    missing_required = [doc for doc in required if doc not in manifest]
    if missing_required:
        return _fail(
            boundary,
            f"{boundary}: WRONG per-seat bundle — the {seat_level} seat's manifest is MISSING its "
            f"per-seat doc(s) {missing_required} (role_variant={role_variant!r} selected the wrong "
            f"bundle — ROLE-RESOLUTION §2 per-level extras; a wrong bundle is a leak)",
        )

    # Another seat's distinguishing per-seat doc must NOT be present.
    forbidden_present = [doc for doc in _forbidden_for(seat_level) if doc in manifest]
    if forbidden_present:
        return _fail(
            boundary,
            f"{boundary}: WRONG per-seat bundle — the {seat_level} seat's manifest carries another "
            f"seat's doc(s) {forbidden_present} (role_variant={role_variant!r} selected the wrong "
            f"bundle — a wrong bundle is a leak; ROLE-RESOLUTION §2)",
        )

    return _ok(boundary)


# ---------------------------------------------------------------------------
# check_all_boundaries — drive the check across every REAL spawn boundary (the L1..L5 LevelConfigs).
# A convenience aggregator (not required by the frozen test) for the harness/CLI to run the whole
# pieces-present sweep deterministically.
# ---------------------------------------------------------------------------

_REAL_LEVELS: tuple[str, ...] = ("L1", "L2", "L3", "L4", "L5", "L5+")  # L5+ added 2026-06-11 —
# the live E1 gate caught the FIRST L5+ spawn with an unresolvable manifest precisely because
# this sweep did not cover it; every registry seat belongs in the deterministic sweep.


def _complete_work_node(node_address: str, *, executor: bool) -> dict:
    """A decision-complete durable work-node pointer (spec pointer always; acceptance for executors).

    Used by the aggregator to drive the REAL boundaries with a complete brief — the SAME shape the
    real spawn writes into the node (DAEMON §6.3).
    """
    wn = {
        "node_address": node_address,
        "workspace": f"/runtime/work/{node_address}",
        "spec_pointer": "design/intent-spec.md",
        "status_md": f"/runtime/work/{node_address}/status.md",
        "log_md": f"/runtime/work/{node_address}/log.md",
        "report_md": f"/runtime/work/{node_address}/report.md",
    }
    if executor:
        wn["frozen_acceptance_ref"] = f"/runtime/work/{node_address}/acceptance.md"
    return wn


def check_all_boundaries() -> list[PiecesResult]:
    """Run the pieces-present check across every REAL spawn boundary (the 5 LevelConfigs).

    Returns one ``PiecesResult`` per boundary. A deterministic, no-model sweep: every result must be
    ``ok`` on the REAL complete corpus; any failure NAMES the missing piece (Inc-18 done-test a).
    """
    results: list[PiecesResult] = []
    for level in _REAL_LEVELS:
        lc = config.LevelConfig.for_level(level)
        executor = _is_executor_seat(lc.role_variant)
        addr = f"proj/{level.lower()}"
        results.append(check_boundary(addr, lc, _complete_work_node(addr, executor=executor)))
    return results


# Tolerant aliases — the frozen test probes several natural entry-point names.
check = check_boundary
check_pieces_present = check_boundary
verify_boundary = check_boundary
verify = check_boundary
