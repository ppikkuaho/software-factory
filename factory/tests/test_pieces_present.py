"""Increment 18 — FROZEN acceptance for the PIECES-PRESENT harness (Phase 6, the first
behavioural-validation instrument). Tests ONLY — NO implementation. RED first: the checker module
``harnessd/pieces_present.py`` does not exist yet.

WHAT THIS INSTRUMENT IS (design/BEHAVIOURAL-VALIDATION.md "Two instruments"): the cheap,
DETERMINISTIC, no-model leak-catcher. For each spawn boundary it assembles the runtime-neutral
brief + load-manifest (via the REAL ``brief.assemble_neutral``) and asserts the brief a level is
handed is COMPLETE — every manifest doc PATH resolves under the read-allow graph, the brief carries
every field the receiving level needs, and ``role_variant`` selects the CORRECT per-seat bundle. It
converts a "silent gap" (a missing piece the faithful agent would route AROUND) into a LOUD,
front-loaded failure at the cheapest possible point.

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN Increment-18 (Phase 6): for each spawn boundary assemble the brief +
    load-manifest and assert every manifest doc PATH resolves; every cross-ref inside a manifest doc
    resolves (no dangling pointer); the brief is decision-complete for the receiving level (spec
    pointer present, frozen ``acceptance.md`` present for executor seats); ``role_variant`` selects
    the CORRECT per-seat manifest (L5<->swe-handbook, L3<->planning-template, #review<->reviewer
    bundle). DONE-TEST: a deliberately removed/renamed role doc OR a missing brief field makes the
    harness FAIL (catches "a piece isn't in place" deterministically). No model.
  * design/BEHAVIOURAL-VALIDATION.md — the "(P) Pieces-present" row: catches "a missing field / a
    dangling ref / a system-it-leverages not in place in the brief+load-manifest a level is handed";
    a leak is a flow that ROUTES AROUND a gap (§"A leak is a flow that ROUTES AROUND a gap").
  * design/ROLE-RESOLUTION.md §2 — the per-seat LOAD-MANIFEST tiers (Shared-always / Per-level /
    Node-local incl. the frozen read-only ``acceptance.md``); seat-variants select different
    manifests (``#exec`` / ``#review`` / ``#test``).
  * design/ROLE-RESOLUTION.md §3 — read-in-place against the harness root (the read-allow graph):
    every manifest doc + every cross-ref is a path relative to the harness root the node reads in
    place.
  * harnessd/spawn/brief.py — ``assemble_neutral(node_address, level_config, work_node)`` ->
    ``NeutralContract`` with ``load_manifest`` (ordered role-doc PATHS), ``spec_pointer``,
    ``frozen_acceptance_ref``, ``node_address``, ``level``, ``role_variant``.
  * harnessd/config.py — ``LevelConfig`` per level (L1..L5); ``role_variant`` is the per-seat
    selector (L5 = ``L5#exec``); ``SYSTEM_PROMPT_FILE`` the shared constant.

BIAS TO REAL (Lesson 7): the checker runs against the REAL operational/ corpus on disk via the REAL
``brief.assemble_neutral`` — NOT mocked. The ONLY synthetic part is the deliberately-broken corpus
for the catch-tests: a temp COPY of the real corpus with a doc removed (the mutant the done-test
requires). The brief and the filesystem are never mocked.

REAL-CORPUS NOTE (a real finding, reported in the build summary, NOT papered over): the real corpus
is COMPLETE for every manifest PATH at every boundary (every load-manifest doc resolves). One
intra-corpus reference is a genuine gap: ``operational/L1/intake-session-template.md`` references
``operational/shared/user-profile.md`` (the persistent per-deployment profile DATA file, defined by
``user-profile-schema.md`` §"Path"), which is not on disk — it is per-deployment runtime data, NOT a
committed manifest doc, and it is a CROSS-REF inside a manifest doc, not a manifest entry. The
dangling-cross-ref FLOOR check therefore excludes per-deployment DATA files (their absence is a
deployment concern, surfaced separately) so the suite is GREEN on the real corpus while the
manifest-resolution check stays strict.

LOAD-BEARING PROPERTIES (each test pins a mutant a wrong checker would let slip):
  * a removed/renamed manifest doc is CAUGHT and NAMED (mutant: skip the resolve check ->
    a removed doc passes -> caught).
  * a missing required brief field — spec_pointer, or acceptance.md for an executor seat — is CAUGHT
    (mutant: skip the decision-complete check -> caught).
  * a WRONG per-seat bundle (L5 handed the L3 manifest) is CAUGHT (mutant: ignore role_variant ->
    L5-gets-L3-manifest passes -> caught).
  * the failure NAMES the specific missing piece (not a silent pass / a generic error).
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import harnessd.config as config
from harnessd.spawn import brief as brief_mod


# ---------------------------------------------------------------------------
# Resolution helpers — the test owns its own ground truth so it does not over-pin
# the builder's checker API. The REAL boundaries (the 5 LevelConfigs) are driven
# through the REAL assemble_neutral; the checker module is driven through a
# tolerant adapter (below) so a reasonable builder shape passes either way.
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """HARNESS_ROOT — the repo root the relative manifest paths resolve against.

    This test file lives at ``<root>/tests/test_pieces_present.py`` (mirrors the adapter's
    ``_harness_root`` = parents up to the repo root).
    """
    return Path(__file__).resolve().parents[1]


# The frozen ``acceptance.md`` is a NODE-LOCAL piece (ROLE-RESOLUTION §2) carried on the work node
# as ``frozen_acceptance_ref`` — executor seats (L5 / #exec / #test) MUST have it.
_EXECUTOR_TOKENS = ("L5",)
_EXECUTOR_SEAT_SUFFIXES = ("#exec", "#test")


def _is_executor_seat(role_variant: str) -> bool:
    token = role_variant.split("#", 1)[0]
    if token in _EXECUTOR_TOKENS:
        return True
    return any(role_variant.endswith(sfx) or sfx in role_variant for sfx in _EXECUTOR_SEAT_SUFFIXES)


def _complete_work_node(node_address: str, *, executor: bool) -> dict:
    """A DECISION-COMPLETE durable work-node pointer (spec pointer always; acceptance for executors)."""
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


# The REAL spawn boundaries that have concrete LevelConfigs in the registry. These are the
# boundaries the done-test (a) must be GREEN on against the REAL corpus.
_REAL_LEVELS = ("L1", "L2", "L3", "L4", "L5")

# Per-seat REQUIRED docs (the bundle role_variant MUST select — ROLE-RESOLUTION §2 per-level extras).
_SEAT_REQUIRED: dict[str, tuple[str, ...]] = {
    "L1": ("operational/L1/handbook.md", "operational/L1/intake-session-template.md"),
    "L3": ("operational/L3/planning-template.md",),
    "L5": ("operational/L5/swe-handbook.md",),
}
# Per-seat FORBIDDEN docs (a wrong bundle for a seat is a leak — L5 must NOT carry L3's template).
_SEAT_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "L5": ("operational/L3/planning-template.md", "operational/L1/handbook.md"),
    "L1": ("operational/L5/swe-handbook.md", "operational/L3/planning-template.md"),
    "L3": ("operational/L5/swe-handbook.md", "operational/L1/handbook.md"),
}


# ---------------------------------------------------------------------------
# The checker module under test — imported lazily so the suite is RED (collect-clean
# but failing) until ``harnessd/pieces_present.py`` exists. A TOLERANT adapter drives
# whatever reasonable entry point the builder ships, so the frozen test pins BEHAVIOUR
# (catches the missing piece + names it), not an exact function signature.
# ---------------------------------------------------------------------------

def _checker():
    """Import the pieces-present checker module (RED until the builder writes it)."""
    return importlib.import_module("harnessd.pieces_present")


def _set_root(checker, monkeypatch, root: Path) -> None:
    """Point the checker's HARNESS_ROOT resolution at ``root`` (the real corpus or a temp mutant).

    Tolerant: monkeypatches whichever root-resolver the builder exposes
    (``harness_root`` / ``HARNESS_ROOT`` / ``_harness_root``). At least one must exist so the
    catch-tests can drive a deliberately-broken temp copy without touching the real tree.
    """
    patched = False
    for name in ("harness_root", "_harness_root"):
        if hasattr(checker, name):
            monkeypatch.setattr(checker, name, lambda root=root: root)
            patched = True
    for name in ("HARNESS_ROOT",):
        if hasattr(checker, name):
            monkeypatch.setattr(checker, name, root)
            patched = True
    assert patched, (
        "the checker must expose a HARNESS_ROOT resolver (harness_root() / _harness_root() / "
        "HARNESS_ROOT) so the deterministic check can be pointed at a temp corpus — "
        "ROLE-RESOLUTION §3 (read-in-place against the harness root)"
    )


def _run_checker(checker, node_address, level_config, work_node, *, root=None, monkeypatch=None):
    """Drive the builder's per-boundary check through a tolerant adapter.

    Tries the natural entry points (``check_boundary`` / ``check`` / ``check_pieces_present``) with
    the assembled-against-real-brief signature. Returns a normalized ``(ok: bool, message: str)``.
    """
    if root is not None and monkeypatch is not None:
        _set_root(checker, monkeypatch, root)

    fn = None
    for name in ("check_boundary", "check", "check_pieces_present", "verify_boundary", "verify"):
        if hasattr(checker, name):
            fn = getattr(checker, name)
            break
    assert fn is not None, (
        "the pieces-present checker must expose a per-boundary check entry point "
        "(check_boundary / check / verify) taking (node_address, level_config, work_node)"
    )

    result = fn(node_address, level_config, work_node)
    return _normalize(result)


def _normalize(result):
    """Normalize a checker result to (ok: bool, message: str) without over-pinning its shape.

    Accepts: a bool; a (bool, msg) tuple; an object with ``.ok`` + a message/details accessor; a
    dict with an ``ok``/``passed`` key. The MESSAGE must name the missing piece for the failing
    cases — that text is what the catch-tests assert on.
    """
    if isinstance(result, bool):
        return result, ""
    if isinstance(result, tuple) and result and isinstance(result[0], bool):
        msg = " ".join(str(x) for x in result[1:])
        return result[0], msg
    if isinstance(result, dict):
        ok = bool(result.get("ok", result.get("passed", False)))
        return ok, repr(result)
    # object-with-attributes shape
    ok_attr = getattr(result, "ok", getattr(result, "passed", None))
    msg = ""
    for accessor in ("fail_message", "message", "reason", "details"):
        val = getattr(result, accessor, None)
        if callable(val):
            try:
                val = val()
            except Exception:
                val = None
        if val:
            msg = str(val)
            break
    if not msg:
        msg = repr(result)
    if ok_attr is None:
        # last resort: truthiness of the whole result
        return bool(result), msg
    return bool(ok_attr), msg


# ===========================================================================
# GROUND-TRUTH checks the TEST performs directly against the REAL assemble_neutral.
# These do NOT depend on the checker module existing — they assert the real brief +
# real corpus are sound, so the satisfiability of the whole increment is anchored on
# REAL artifacts (Lesson 7). If these go RED, that is a REAL corpus/brief finding.
# ===========================================================================

@pytest.mark.parametrize("level", _REAL_LEVELS)
def test_real_corpus_every_manifest_doc_resolves(level):
    """(done-test a) For every REAL spawn boundary, every load-manifest doc PATH resolves on disk
    under the harness root (the read-allow graph, ROLE-RESOLUTION §3).

    This is the load-bearing completeness check: a manifest doc the level is handed that does NOT
    resolve is "a system it is meant to leverage not in place" (BEHAVIOURAL-VALIDATION). Driven
    through the REAL brief + REAL corpus — not mocked.
    """
    root = _repo_root()
    lc = config.LevelConfig.for_level(level)
    executor = _is_executor_seat(lc.role_variant)
    contract = brief_mod.assemble_neutral(
        f"proj/{level.lower()}", lc, _complete_work_node(f"proj/{level.lower()}", executor=executor)
    )

    assert contract.load_manifest, f"{level}: the load-manifest must be non-empty"
    missing = [doc for doc in contract.load_manifest if not (root / doc.split("#", 1)[0]).is_file()]
    assert not missing, (
        f"{level}: load-manifest docs do not resolve on disk under the harness root: {missing} "
        f"(a manifest doc not present is a missing piece the harness must catch — "
        f"ROLE-RESOLUTION §3 read-allow graph)"
    )


@pytest.mark.parametrize("level", _REAL_LEVELS)
def test_real_corpus_brief_is_decision_complete(level):
    """(done-test, field-completeness) The assembled brief carries every field the receiving level
    needs: the node identity/address, the spec pointer, and — for EXECUTOR seats — the frozen
    ``acceptance.md`` reference (ROLE-RESOLUTION §2 node-local tier; §6.3)."""
    lc = config.LevelConfig.for_level(level)
    executor = _is_executor_seat(lc.role_variant)
    addr = f"proj/{level.lower()}"
    contract = brief_mod.assemble_neutral(addr, lc, _complete_work_node(addr, executor=executor))

    assert contract.node_address == addr, f"{level}: the brief must carry the node identity/address"
    assert contract.level, f"{level}: the brief must carry the level identity"
    assert contract.spec_pointer, f"{level}: the brief must carry the spec_pointer (decision-complete)"
    if executor:
        assert contract.frozen_acceptance_ref, (
            f"{level}: an EXECUTOR seat's brief must carry the frozen acceptance.md reference "
            f"(ROLE-RESOLUTION §2 node-local tier — the spec-anchored target)"
        )


def test_real_corpus_role_variant_selects_correct_per_seat_bundle():
    """(done-test, per-seat selection) ``role_variant`` selects the CORRECT per-seat manifest:
    L5 includes the swe-handbook tier; L3 the planning-template; L1 the intake-session-template —
    and a seat does NOT carry another seat's bundle (a wrong bundle is a leak)."""
    root = _repo_root()
    for level, required in _SEAT_REQUIRED.items():
        lc = config.LevelConfig.for_level(level)
        contract = brief_mod.assemble_neutral(
            f"proj/{level.lower()}", lc, _complete_work_node(f"proj/{level.lower()}", executor=_is_executor_seat(lc.role_variant))
        )
        for doc in required:
            assert doc in contract.load_manifest, (
                f"{level}: role_variant must select its per-seat doc {doc!r} into the manifest "
                f"(ROLE-RESOLUTION §2 per-level extras)"
            )
            assert (root / doc).is_file(), f"{level}: required per-seat doc {doc!r} must resolve on disk"
        for forbidden in _SEAT_FORBIDDEN.get(level, ()):
            assert forbidden not in contract.load_manifest, (
                f"{level}: a wrong per-seat bundle is a leak — {level} must NOT carry {forbidden!r}"
            )


def test_real_corpus_manifest_cross_refs_resolve_floor():
    """(v1 floor) No DANGLING intra-corpus cross-ref: scan each REAL manifest doc for obvious
    ``operational/…`` / ``design/…`` references and assert they resolve under the harness root
    (a referenced doc that points at a non-existent doc is a missing piece — IMPLEMENTATION-PLAN
    Inc-18). Per-deployment runtime DATA files are EXCLUDED (their absence is a deployment concern,
    surfaced separately — see module docstring / REAL-CORPUS NOTE), keeping this tractable and not a
    full link-checker.
    """
    import re

    root = _repo_root()
    ref_re = re.compile(r"(?:operational|design)/[A-Za-z0-9_./+#-]+\.md")
    # Per-deployment DATA (not a committed manifest doc): excluded from the floor.
    deployment_data = {"operational/shared/user-profile.md"}

    seen_docs: set[str] = set()
    for level in _REAL_LEVELS:
        lc = config.LevelConfig.for_level(level)
        contract = brief_mod.assemble_neutral(
            f"proj/{level.lower()}", lc, _complete_work_node(f"proj/{level.lower()}", executor=_is_executor_seat(lc.role_variant))
        )
        seen_docs.update(d.split("#", 1)[0] for d in contract.load_manifest)

    dangling: list[str] = []
    for doc in sorted(seen_docs):
        p = root / doc
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for ref in ref_re.findall(text):
            refbase = ref.split("#", 1)[0]
            if refbase in deployment_data:
                continue
            if not (root / refbase).is_file():
                dangling.append(f"{doc} -> {ref}")

    assert not dangling, (
        "dangling intra-corpus cross-ref(s) in manifest docs (a referenced doc that does not "
        f"resolve is a missing piece — Inc-18): {sorted(set(dangling))}"
    )


# ===========================================================================
# DONE-TEST (a): the CHECKER passes on the REAL complete operational/ corpus for every boundary.
# Driven through the builder's checker (RED until it exists), against the REAL corpus + REAL brief.
# ===========================================================================

@pytest.mark.parametrize("level", _REAL_LEVELS)
def test_checker_passes_on_real_corpus(level):
    """(done-test a) The pieces-present CHECKER passes on the REAL complete corpus for every
    boundary — every manifest doc resolves, the brief is decision-complete, the per-seat bundle is
    correct. No model; deterministic. (RED until harnessd/pieces_present.py exists.)"""
    checker = _checker()
    lc = config.LevelConfig.for_level(level)
    executor = _is_executor_seat(lc.role_variant)
    addr = f"proj/{level.lower()}"
    ok, msg = _run_checker(checker, addr, lc, _complete_work_node(addr, executor=executor))
    assert ok, f"{level}: the checker must PASS on the REAL complete corpus, got failure: {msg}"


# ===========================================================================
# DONE-TEST (b): a DELIBERATELY removed/renamed manifest doc makes the checker FAIL LOUD,
# NAMING the missing doc. The mutant is a temp COPY of the REAL corpus with one doc removed.
# Mutant killed: a checker that skips the resolve check -> a removed doc passes -> NOT caught.
# ===========================================================================

def test_checker_fails_loud_on_removed_manifest_doc(tmp_path, monkeypatch):
    """(done-test b) Remove a real manifest doc from a temp copy of the corpus -> the checker FAILS
    and NAMES the missing doc. Catches "a piece isn't in place" deterministically (Inc-18)."""
    checker = _checker()

    # Build a temp COPY of the real corpus (operational/ + design/, both read-allowed roots).
    src = _repo_root()
    root = tmp_path / "harness"
    (root / "operational").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / "operational", root / "operational")
    shutil.copytree(src / "design", root / "design")

    # Baseline: the temp copy is COMPLETE -> the checker PASSES (proves the mutation, not the copy,
    # is what flips it).
    lc = config.LevelConfig.for_level("L5")
    addr = "proj/l5"
    wn = _complete_work_node(addr, executor=True)
    ok_base, msg_base = _run_checker(checker, addr, lc, wn, root=root, monkeypatch=monkeypatch)
    assert ok_base, f"the temp copy of the REAL corpus must PASS before mutation, got: {msg_base}"

    # MUTANT: remove the L5 per-seat doc (swe-handbook.md) that L5's manifest names.
    removed = "operational/L5/swe-handbook.md"
    (root / removed).unlink()

    ok, msg = _run_checker(checker, addr, lc, wn, root=root, monkeypatch=monkeypatch)
    assert not ok, (
        "a removed manifest doc must make the checker FAIL (mutant: skipping the resolve check lets "
        "a removed doc pass -> the silent-gap leak is missed)"
    )
    assert "swe-handbook" in msg, (
        f"the failure must NAME the specific missing doc ({removed}), not fail generically; got: {msg}"
    )


def test_checker_fails_loud_on_renamed_manifest_doc(tmp_path, monkeypatch):
    """(done-test b, rename variant) RENAMING a manifest doc (so the manifest PATH no longer
    resolves) makes the checker FAIL naming the now-missing PATH — a rename is a remove from the
    manifest's point of view."""
    checker = _checker()
    src = _repo_root()
    root = tmp_path / "harness"
    shutil.copytree(src / "operational", root / "operational")
    shutil.copytree(src / "design", root / "design")

    # Rename the L3 planning-template -> the L3 manifest's path dangles.
    lc = config.LevelConfig.for_level("L3")
    addr = "proj/l3"
    wn = _complete_work_node(addr, executor=False)
    renamed_from = "operational/L3/planning-template.md"
    (root / renamed_from).rename(root / "operational/L3/planning-template.RENAMED.md")

    ok, msg = _run_checker(checker, addr, lc, wn, root=root, monkeypatch=monkeypatch)
    assert not ok, "a renamed (so unresolved) manifest doc must make the checker FAIL"
    assert "planning-template" in msg, (
        f"the failure must NAME the now-missing manifest path ({renamed_from}); got: {msg}"
    )


# ===========================================================================
# DONE-TEST (c): a missing REQUIRED brief field makes the checker FAIL.
#   (c1) a seat with NO spec_pointer.
#   (c2) an EXECUTOR seat with NO frozen acceptance.md reference.
# Mutant killed: a checker that skips the decision-complete check -> caught.
# ===========================================================================

def test_checker_fails_on_missing_spec_pointer():
    """(done-test c1) A brief with NO spec_pointer is NOT decision-complete -> the checker FAILS,
    naming the missing field. Against the REAL corpus (the manifest docs all resolve — only the
    field is dropped)."""
    checker = _checker()
    lc = config.LevelConfig.for_level("L3")
    addr = "proj/l3"
    wn = _complete_work_node(addr, executor=False)
    del wn["spec_pointer"]  # drop the required field

    ok, msg = _run_checker(checker, addr, lc, wn)
    assert not ok, (
        "a brief missing the spec_pointer must make the checker FAIL (mutant: skipping the "
        "decision-complete check -> an under-specified brief passes -> silent gap)"
    )
    assert "spec_pointer" in msg or "spec pointer" in msg.lower(), (
        f"the failure must NAME the missing required field (spec_pointer); got: {msg}"
    )


def test_checker_fails_on_executor_seat_missing_acceptance():
    """(done-test c2) An EXECUTOR seat (L5) whose brief carries NO frozen acceptance.md reference
    must make the checker FAIL — the executor's spec-anchored target is its primary anchor
    (ROLE-RESOLUTION §2 node-local tier)."""
    checker = _checker()
    lc = config.LevelConfig.for_level("L5")
    addr = "proj/l5"
    wn = _complete_work_node(addr, executor=False)  # NO frozen_acceptance_ref for an executor seat

    ok, msg = _run_checker(checker, addr, lc, wn)
    assert not ok, (
        "an executor seat with no frozen acceptance.md reference must make the checker FAIL "
        "(mutant: skipping the executor-acceptance check -> a rudderless executor passes)"
    )
    assert "accept" in msg.lower(), (
        f"the failure must NAME the missing acceptance reference; got: {msg}"
    )


# ===========================================================================
# DONE-TEST (d): a WRONG per-seat bundle makes the checker FAIL.
# The mutant: L5 handed the L3 manifest (the planning-template instead of / in addition to the
# swe-handbook). Mutant killed: a checker that ignores role_variant -> L5-gets-L3 passes -> leak.
# ===========================================================================

def test_checker_fails_on_wrong_per_seat_bundle(tmp_path, monkeypatch):
    """(done-test d) An L5 seat handed the L3 manifest (a wrong per-seat bundle) makes the checker
    FAIL — the per-seat selection (role_variant) is load-bearing; a wrong bundle is a leak.

    The mutant forces a wrong manifest by passing a level_config whose role_variant resolves the L3
    bundle while the seat is L5. We assert the checker rejects an L5 seat carrying the L3
    planning-template (or missing the L5 swe-handbook)."""
    checker = _checker()

    # An "L5 seat" config that LIES about its bundle: role_variant says L3, so assemble_neutral
    # builds the L3 manifest — but the seat is meant to be an L5 executor. A checker that ignores
    # role_variant-vs-seat consistency would pass this; the correct checker must catch it.
    real_l5 = config.LevelConfig.for_level("L5")
    mis_seat = replace(real_l5, role_variant="L3")  # wrong bundle for an L5 model/runtime seat

    addr = "proj/l5"
    wn = _complete_work_node(addr, executor=True)

    contract = brief_mod.assemble_neutral(addr, mis_seat, wn)
    # Ground truth: this contract has the L3 bundle (planning-template), NOT the L5 swe-handbook.
    assert "operational/L3/planning-template.md" in contract.load_manifest
    assert "operational/L5/swe-handbook.md" not in contract.load_manifest

    ok, msg = _run_checker(checker, addr, mis_seat, wn)
    assert not ok, (
        "an L5 seat handed the L3 manifest (wrong per-seat bundle) must make the checker FAIL "
        "(mutant: ignoring role_variant -> L5-gets-L3-manifest passes -> the leak slips)"
    )
    # The failure must point at the wrong bundle (the L3 doc present where it shouldn't be, or the
    # L5 doc missing where it should be).
    assert ("planning-template" in msg) or ("swe-handbook" in msg) or ("bundle" in msg.lower()), (
        f"the failure must NAME the wrong-bundle piece (the L3 doc present / the L5 doc absent); got: {msg}"
    )
