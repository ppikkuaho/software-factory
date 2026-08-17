"""Increment 0 — load-bearing STRENGTHENING (mutation-review gate).

The Increment-0 mutation review found that the original config probes were presence-only:
a wrong impl with `role_variant` collapsed to one constant, an empty `tool_manifest`, or a
garbage `runtime` PASSED all tests — so "tests pass == correct" did NOT hold for those seats.
These tests add the missing teeth, so the canonical regressions FAIL:

  - role_variant hardcoded to one constant across all levels  -> FAIL (must vary, encode its level)
  - tool_manifest = ()                                        -> FAIL (must be non-empty)
  - runtime = "XXX"                                           -> FAIL (must be a known runtime; E32 split)
  - LIVENESS_STATES reordered / working==waiting merged       -> FAIL (LOCKED 4-value enum)

Authoritative: DAEMON §3.2 (role_variant is the PER-seat selector), runtime-and-model-map E31/E32
(L1-L4 = opus/claude-code; L5 = gpt-5.6-sol/codex — the 2026-07-16 L5 Codex-path ruling, which
reversed the 2026-07-13 Sol/CLIProxy unification's runtime dimension), agent-lifecycle.md
(LIVENESS_STATES LOCKED 4-value; working!=waiting is load-bearing for the §5.4 coordinator
roll-up).
"""

import harnessd.config as config
import harnessd.states as states

LEVELS = ["L1", "L2", "L3", "L4", "L5"]
KNOWN_RUNTIMES = {"claude-code", "codex"}
_MISSING = object()


def _level_config(level):
    """Resolve a LevelConfig tolerantly (getter, then registry)."""
    fn = getattr(config, "get_level_config", None) or getattr(config, "level_config", None)
    if callable(fn):
        lc = fn(level)
        if lc is not None:
            return lc
    LC = getattr(config, "LevelConfig", None)
    if LC is not None and hasattr(LC, "for_level"):
        return LC.for_level(level)
    for regname in ("LEVEL_CONFIGS", "CONFIGS", "LEVELS"):
        reg = getattr(config, regname, None)
        if isinstance(reg, dict) and level in reg:
            return reg[level]
    raise AssertionError(f"no LevelConfig resolvable for {level}")


def _field(obj, name):
    if isinstance(obj, dict):
        return obj.get(name, _MISSING)
    return getattr(obj, name, _MISSING)


# --------------------------------------------------------------------------------------
# role_variant — the per-seat selector. MUST vary across levels and encode the level token.
# (Mutants caught: role_variant == "L1" for all levels; role_variant == "".)
# --------------------------------------------------------------------------------------

def test_role_variant_varies_across_levels():
    variants = [_field(_level_config(lv), "role_variant") for lv in LEVELS]
    assert all(v is not _MISSING for v in variants), "every level must carry role_variant"
    assert all(isinstance(v, str) and v.strip() for v in variants), f"role_variant must be a non-empty string, got {variants}"
    assert len(set(variants)) > 1, (
        f"role_variant is the PER-seat selector — it MUST differ across levels, "
        f"a single constant collapses every seat to one role: {variants}"
    )


def test_role_variant_encodes_its_level():
    for lv in LEVELS:
        rv = _field(_level_config(lv), "role_variant")
        assert lv in str(rv), f"{lv}'s role_variant should encode its level token, got {rv!r}"


# --------------------------------------------------------------------------------------
# tool_manifest — MUST be a non-empty sequence (an empty manifest ships a no-tools agent).
# --------------------------------------------------------------------------------------

def test_tool_manifest_nonempty_per_level():
    for lv in LEVELS:
        tm = _field(_level_config(lv), "tool_manifest")
        assert tm is not _MISSING, f"{lv} missing tool_manifest"
        assert hasattr(tm, "__len__") and len(tm) > 0, f"{lv} tool_manifest must be non-empty, got {tm!r}"


# --------------------------------------------------------------------------------------
# runtime — MUST be a known runtime. Since the 2026-07-16 L5 CODEX-PATH RULING, L5 runs
# GPT-5.5 on the NATIVE codex runtime (no CLIProxy — the 2026-07-13 Sol wiring was removed);
# L1-L4 and every + review seat run Opus 5.0 on claude-code. Judgment diversity lives in
# BOTH the model and the runtime (L5 = GPT-5.5/codex, L5+ = Opus/claude-code).
# (Mutant caught: runtime == "XXX".)
# --------------------------------------------------------------------------------------

def test_runtime_known_and_follows_e32_split():
    for lv in LEVELS:
        rt = _field(_level_config(lv), "runtime")
        assert rt in KNOWN_RUNTIMES, f"{lv} runtime {rt!r} is not a known runtime {KNOWN_RUNTIMES}"
    for lv in ["L1", "L2", "L3", "L4"]:
        assert _field(_level_config(lv), "runtime") == "claude-code", f"{lv} must run on claude-code (E32)"
    assert _field(_level_config("L5"), "runtime") == "codex", (
        "L5 runs on the NATIVE codex runtime (owner ruling 2026-07-16: the L5 Codex path, no "
        "CLIProxy, single warm runner)"
    )
    assert _field(_level_config("L5+"), "runtime") == "claude-code", (
        "L5+ (the M52 reviewer) runs Opus on claude-code — a DIFFERENT model AND runtime from the "
        "GPT-5.6-Sol/codex L5 it reviews (judgment diversity, 2026-07-16 ruling)"
    )


def test_model_follows_e32_split():
    models = {lv: _field(_level_config(lv), "model") for lv in LEVELS}
    assert all(isinstance(m, str) and m.strip() for m in models.values()), f"every model seat non-empty, got {models}"
    # L1-L4 share one model family; L5 is the DISTINCT GPT-5.6 Sol executor seat on the codex
    # runtime (2026-07-16 Codex-path ruling; model corrected to gpt-5.6-sol 2026-07-17).
    assert len({models[lv] for lv in ["L1", "L2", "L3", "L4"]}) == 1, f"L1-L4 share one model, got {models}"
    assert models["L5"] == "gpt-5.6-sol", (
        f"L5 is the executor seat (GPT-5.6 Sol on the native codex runtime — owner correction 2026-07-17), got {models}"
    )


# --------------------------------------------------------------------------------------
# LIVENESS_STATES — the LOCKED 4-value enum. Order + values fixed; working != waiting.
# (Mutants caught: reordering; merging working/waiting; dropping a value.)
# --------------------------------------------------------------------------------------

def test_liveness_states_locked():
    assert tuple(states.LIVENESS_STATES) == ("working", "waiting", "idle", "dead"), (
        f"LIVENESS_STATES is the LOCKED 4-value enum, got {states.LIVENESS_STATES!r}"
    )
    assert len(set(states.LIVENESS_STATES)) == 4, "working/waiting/idle/dead must be four DISTINCT values"
