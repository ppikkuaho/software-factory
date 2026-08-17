"""Increment 0 Done-test (config seats): harnessd/config.py exposes the config-time
seats the role-resolution model + spawn machinery require, so nothing downstream hardcodes them.

Authoritative sources:
  - IMPLEMENTATION-PLAN.md §1 module table, `harnessd/config.py` row:
      "LevelConfig per level (model/runtime/reasoning_effort/role_variant/tool_manifest) plus the CONSTANT
       system_prompt_file = operational/shared/system-prompt.md ... the per-state suspicion
       windows W(state) (placeholder constants in v1, see FORK-W), the pinned-binary version/hash."
  - §3.2 binding "Spawn fact (H40)": system_prompt_file is the CONSTANT shared
    operational/shared/system-prompt.md (runtime-global, IDENTICAL across role_variants L1..L5);
    role_variant is the per-seat selector.
  - FORK-W: v1 placeholder constants e.g. W_working=120s, W_waiting_on_child=600s, W_writing_final=60s.
  - PINNED-CC.md: pinned Claude Code Version 2.1.152.

NOTE ON STRICTNESS: §2.3 freezes states.py's interface names but the plan does NOT freeze
config.py's exact attribute names. These tests therefore probe the SEATS tolerantly (accepting a
module constant, a dict entry, a dataclass field, or a per-level accessor) but pin the VALUES the
plan fixes (the shared-prompt path, version 2.1.152, the three W numbers). A config.py that simply
forgets a seat MUST still fail.

These tests fail RED until harnessd/config.py exists with the seats.
"""

import pytest

# Direct import (NOT importorskip): until the builder creates harnessd/config.py this is a
# collection-time ImportError, which pytest reports as a RED error for every test in the file.
# A skip would wrongly read as "the config seats are satisfied".
import harnessd.config as config


# The CONSTANT shared system prompt path, identical across L1..L5 (a runtime-global).
SHARED_SYSTEM_PROMPT = "operational/shared/system-prompt.md"

# The five seats LevelConfig must carry (per the §1 module table).
LEVELS = ["L1", "L2", "L3", "L4", "L5"]

# FORK-W placeholder constants (seconds).
EXPECTED_W = {
    "working": 120,
    "waiting_on_child": 600,
    "writing_final": 60,
}

# PINNED-CC pinned version.
PINNED_VERSION = "2.1.152"


# --------------------------------------------------------------------------------------
# Tolerant probes — find a seat across the reasonable shapes a builder might pick.
# --------------------------------------------------------------------------------------

_MISSING = object()


def _module_value(*names):
    """First module-level attribute among `names` whose value is not None, else _MISSING."""
    for n in names:
        if hasattr(config, n):
            v = getattr(config, n)
            if v is not None:
                return v
    return _MISSING


def _get_level_config(level):
    """Resolve a per-level LevelConfig instance via the most plausible accessors.

    Accepts: a factory `LevelConfig(level)` / `LevelConfig.for_level(level)`,
    a registry mapping (`LEVEL_CONFIGS`/`LEVELS`/`CONFIGS`) keyed by level,
    or a module getter `get_level_config(level)` / `level_config(level)`.
    """
    # 1) Module-level getter functions.
    for fname in ("get_level_config", "level_config", "for_level", "resolve_level"):
        fn = getattr(config, fname, None)
        if callable(fn):
            try:
                lc = fn(level)
                if lc is not None:
                    return lc
            except Exception:
                pass

    # 2) A registry mapping keyed by level.
    for regname in ("LEVEL_CONFIGS", "LEVELS", "CONFIGS", "LEVEL_CONFIG", "config"):
        reg = getattr(config, regname, None)
        if isinstance(reg, dict) and level in reg:
            return reg[level]

    # 3) LevelConfig itself as a factory / classmethod.
    LC = getattr(config, "LevelConfig", None)
    if LC is not None:
        for cm in ("for_level", "of", "by_level", "get"):
            meth = getattr(LC, cm, None)
            if callable(meth):
                try:
                    lc = meth(level)
                    if lc is not None:
                        return lc
                except Exception:
                    pass
        # Constructor by level token.
        try:
            return LC(level)
        except Exception:
            pass

    return _MISSING


def _field(obj, *names):
    """First present, non-None field among `names`, treating obj as attrs OR a mapping."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj and obj[n] is not None:
                return obj[n]
        elif hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return _MISSING


def _stringish(v):
    return v if isinstance(v, str) else str(v)


# --------------------------------------------------------------------------------------
# LevelConfig type exists.
# --------------------------------------------------------------------------------------

def test_level_config_symbol_exists():
    assert hasattr(config, "LevelConfig"), "config.LevelConfig must exist (the per-level seat type)"


@pytest.mark.parametrize("level", LEVELS)
def test_level_config_resolvable_for_each_level(level):
    """A LevelConfig is obtainable for every L1..L5 seat."""
    lc = _get_level_config(level)
    assert lc is not _MISSING, f"no LevelConfig resolvable for {level}"


# --------------------------------------------------------------------------------------
# LevelConfig carries model / runtime / reasoning_effort / role_variant / tool_manifest.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("level", LEVELS)
def test_level_config_has_model_seat(level):
    lc = _get_level_config(level)
    assert lc is not _MISSING, f"no LevelConfig for {level}"
    assert _field(lc, "model") is not _MISSING, f"{level} LevelConfig missing `model` seat"


@pytest.mark.parametrize("level", LEVELS)
def test_level_config_has_runtime_seat(level):
    lc = _get_level_config(level)
    assert lc is not _MISSING, f"no LevelConfig for {level}"
    assert _field(lc, "runtime") is not _MISSING, f"{level} LevelConfig missing `runtime` seat"


@pytest.mark.parametrize("level", LEVELS)
def test_level_config_has_role_variant_seat(level):
    """role_variant is the per-seat selector the chokepoint resolves to a load-manifest/role
    bundle (§3.2 Spawn fact). It MUST be a per-level field, not a global constant."""
    lc = _get_level_config(level)
    assert lc is not _MISSING, f"no LevelConfig for {level}"
    rv = _field(lc, "role_variant")
    assert rv is not _MISSING, f"{level} LevelConfig missing `role_variant` per-seat selector"


@pytest.mark.parametrize("level", LEVELS)
def test_level_config_has_tool_manifest_seat(level):
    lc = _get_level_config(level)
    assert lc is not _MISSING, f"no LevelConfig for {level}"
    assert _field(lc, "tool_manifest") is not _MISSING, f"{level} LevelConfig missing `tool_manifest` seat"


def test_reasoning_effort_is_explicit_across_every_production_seat():
    expected_levels = {
        "L1": "xhigh",
        "L2": "xhigh",
        "L3": "xhigh",
        "L4": "high",
        "L5": "high",
        "L2+": "high",
        "L3+": "high",
        "L4+": "high",
        "L5+": "high",
    }
    assert {
        level: config.LEVEL_CONFIGS[level].reasoning_effort
        for level in expected_levels
    } == expected_levels
    assert {
        seat: level_config.reasoning_effort
        for seat, level_config in config.SEMANTIC_CELL_LEVEL_CONFIGS.items()
    } == {
        "reconstruction-verification": "high",
        "reconstruction-construction": "high",
        "comparator": "high",
        "coherence": "high",
        "atomization": "high",
    }
    assert {
        seat: level_config.reasoning_effort
        for seat, level_config in config.PRODUCT_PROBE_LEVEL_CONFIGS.items()
    } == {
        "user-simulation": "high",
        "performance-robustness": "high",
    }


def test_sparse_level_config_defaults_reasoning_effort_to_high():
    sparse = config.LevelConfig(
        level="test",
        model="test-model",
        runtime="test-runtime",
        role_variant="test#exec",
        tool_manifest=(),
    )

    assert sparse.reasoning_effort == "high"


# --------------------------------------------------------------------------------------
# SYSTEM_PROMPT_FILE is the CONSTANT shared base path — identical across L1..L5.
# --------------------------------------------------------------------------------------

def test_system_prompt_file_constant_value():
    """The shared prompt base is the CONSTANT operational/shared/system-prompt.md.
    It may live as a module constant OR on each LevelConfig — but its VALUE is fixed."""
    val = _module_value("SYSTEM_PROMPT_FILE", "system_prompt_file", "SHARED_SYSTEM_PROMPT_FILE")
    if val is _MISSING:
        # Fall back to a per-level field, but it must still carry the constant value.
        lc = _get_level_config("L1")
        assert lc is not _MISSING, "system_prompt_file seat not found at module or level scope"
        val = _field(lc, "system_prompt_file", "SYSTEM_PROMPT_FILE")
        assert val is not _MISSING, "system_prompt_file seat not found"
    assert _stringish(val).endswith(SHARED_SYSTEM_PROMPT), (
        f"system_prompt_file must be the shared constant {SHARED_SYSTEM_PROMPT!r}, got {val!r}"
    )


def test_system_prompt_file_identical_across_all_levels():
    """The whole point of the CONSTANT: the same shared prompt for every level. A per-level
    role path here would be the bug this seat exists to prevent."""
    values = set()
    for level in LEVELS:
        lc = _get_level_config(level)
        if lc is _MISSING:
            continue
        v = _field(lc, "system_prompt_file", "SYSTEM_PROMPT_FILE")
        if v is _MISSING:
            # Allowed: not a per-level field, lives as a single module constant instead.
            v = _module_value("SYSTEM_PROMPT_FILE", "system_prompt_file", "SHARED_SYSTEM_PROMPT_FILE")
        assert v is not _MISSING, f"{level} cannot resolve system_prompt_file"
        values.add(_stringish(v))
    assert len(values) == 1, f"system_prompt_file must be IDENTICAL across L1..L5, got {values}"
    assert next(iter(values)).endswith(SHARED_SYSTEM_PROMPT)


def test_system_prompt_file_is_not_a_per_level_role_path():
    """Guard the specific regression: it must NOT point at operational/L{n}/... role files."""
    for level in LEVELS:
        lc = _get_level_config(level)
        if lc is _MISSING:
            continue
        v = _field(lc, "system_prompt_file", "SYSTEM_PROMPT_FILE")
        if v is _MISSING:
            continue
        s = _stringish(v)
        assert "/L1/" not in s and "/L2/" not in s and "/L3/" not in s \
            and "/L4/" not in s and "/L5/" not in s, \
            f"system_prompt_file for {level} looks like a per-level role path: {s!r}"


# --------------------------------------------------------------------------------------
# W(state) suspicion-window placeholder constants (FORK-W).
# --------------------------------------------------------------------------------------

def _resolve_W():
    """Return {state: seconds} from whatever shape config exposes the windows in.

    Accepts: module constants W_working / W_waiting_on_child / W_writing_final,
    a mapping W / SUSPICION_WINDOWS / W_WINDOWS keyed by state,
    or a callable W(state) / suspicion_window(state).
    """
    out = {}

    # Mapping form.
    for mapname in ("W", "SUSPICION_WINDOWS", "W_WINDOWS", "WINDOWS"):
        m = getattr(config, mapname, None)
        if isinstance(m, dict):
            for state in EXPECTED_W:
                if state in m:
                    out[state] = m[state]

    # Callable form W(state).
    for fname in ("W", "suspicion_window", "window_for"):
        fn = getattr(config, fname, None)
        if callable(fn):
            for state in EXPECTED_W:
                try:
                    v = fn(state)
                    if v is not None:
                        out.setdefault(state, v)
                except Exception:
                    pass

    # Per-constant form W_working etc.
    const_map = {
        "working": ("W_working", "W_WORKING"),
        "waiting_on_child": ("W_waiting_on_child", "W_WAITING_ON_CHILD"),
        "writing_final": ("W_writing_final", "W_WRITING_FINAL"),
    }
    for state, names in const_map.items():
        v = _module_value(*names)
        if v is not _MISSING:
            out.setdefault(state, v)

    return out


def test_w_state_placeholder_constants_present():
    """All three FORK-W placeholder windows are exposed as config seats (not hardcoded inline)."""
    resolved = _resolve_W()
    missing = [s for s in EXPECTED_W if s not in resolved]
    assert not missing, f"W(state) placeholder constants missing for: {missing}"


@pytest.mark.parametrize("state,seconds", sorted(EXPECTED_W.items()))
def test_w_state_placeholder_values(state, seconds):
    """The v1 placeholder values: W_working=120s, W_waiting_on_child=600s, W_writing_final=60s."""
    resolved = _resolve_W()
    assert state in resolved, f"W({state}) not exposed"
    got = resolved[state]
    got_num = got.total_seconds() if hasattr(got, "total_seconds") else got
    assert int(got_num) == seconds, f"W({state}) placeholder should be {seconds}s, got {got!r}"


# --------------------------------------------------------------------------------------
# Pinned-binary version/hash seat.
# --------------------------------------------------------------------------------------

def test_pinned_binary_version_seat():
    """The pinned Claude Code version (2.1.152, PINNED-CC) is a config seat, not hardcoded
    at the spawn site."""
    val = _module_value(
        "PINNED_BINARY_VERSION", "PINNED_VERSION", "PINNED_CC_VERSION",
        "pinned_binary_version", "BINARY_VERSION", "CLAUDE_CODE_VERSION",
    )
    if val is _MISSING:
        # May be nested under a pinned-binary descriptor object/dict.
        for holder in ("PINNED_BINARY", "PINNED_CC", "pinned_binary"):
            h = getattr(config, holder, None)
            if h is not None:
                val = _field(h, "version", "pinned_version")
                if val is not _MISSING:
                    break
    assert val is not _MISSING, "pinned-binary VERSION seat not found in config"
    assert _stringish(val) == PINNED_VERSION, f"pinned version must be {PINNED_VERSION!r}, got {val!r}"


def test_pinned_binary_hash_seat_exists():
    """A pinned-binary HASH seat exists (value may be a v1 placeholder/None, but the SEAT
    must be present so the chokepoint's verify_binary(version=..., hash=...) has somewhere
    to read from — IMPLEMENTATION-PLAN §2.11 / §1 config row)."""
    # Module-level hash constant.
    names = (
        "PINNED_BINARY_HASH", "PINNED_HASH", "PINNED_CC_HASH",
        "pinned_binary_hash", "BINARY_HASH", "CLAUDE_CODE_HASH",
    )
    found = any(hasattr(config, n) for n in names)

    # Or nested on a pinned-binary descriptor (attr present even if value is a placeholder).
    if not found:
        for holder in ("PINNED_BINARY", "PINNED_CC", "pinned_binary"):
            h = getattr(config, holder, None)
            if h is None:
                continue
            if isinstance(h, dict):
                if any(k in h for k in ("hash", "binary_hash", "pinned_hash")):
                    found = True
                    break
            elif any(hasattr(h, k) for k in ("hash", "binary_hash", "pinned_hash")):
                found = True
                break

    assert found, "pinned-binary HASH seat not found in config (verify_binary has nowhere to read the pinned hash)"
