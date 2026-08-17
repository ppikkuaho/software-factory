"""Config-time seats the rest of the harness must NOT hardcode.

Authoritative sources:
  - IMPLEMENTATION-PLAN §1 module table (`harnessd/config.py` row): `LevelConfig`
    per level (model / runtime / reasoning_effort / role_variant / tool_manifest), the CONSTANT
    `system_prompt_file = operational/shared/system-prompt.md`, the per-state
    suspicion windows `W(state)` (placeholder constants in v1, FORK-W), and the
    pinned-binary version/hash.
  - DAEMON §3.2 / §6.2: `SYSTEM_PROMPT_FILE` is the ONE shared minimal prompt
    used as the base of every composed Claude identity bundle, byte-identical
    L1–L5 (a runtime-global, NOT a per-level role path). `role_variant` is the
    PER-binding selector that varies by seat.
  - operational/shared/runtime-and-model-map.md (E31/E32 assignment table): the
    per-level model + runtime config snapshot.
  - FORK-W (WATCHDOG §8): v1 placeholder windows W_working=120s,
    W_waiting_on_child=600s, W_writing_final=60s.
  - PINNED-CC.md: pinned Claude Code version 2.1.152.

"Commissioning tunes these without a code change" — they are config seats, not
inline constants buried at the spawn site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# ---------------------------------------------------------------------------
# CONSTANT shared system prompt base (DAEMON §3.2/§6.2; ROLE-RESOLUTION §1).
#
# The ONE shared minimal base document, byte-identical for L1–L5 — a runtime-global,
# NOT a per-level role path. Claude-Code seats compose it with the selected identity
# trio into `.identity-prompt.md`; the per-seat selection is carried by `role_variant`,
# never by this path.
# ---------------------------------------------------------------------------

# NOTE (resolution contract, for Increment 9 / the Claude-Code adapter): this is
# relative-to-HARNESS-ROOT. The daemon is launchd-managed (§2.2) with a CWD that
# is NOT guaranteed to be the repo root, so the adapter MUST join this against the
# resolved HARNESS_ROOT (the same root used for CLAUDE_CONFIG_DIR=$HARNESS/.cc-pinned/config)
# before composing the per-spawn prompt; never pass it raw to a launchd-CWD process.
SYSTEM_PROMPT_FILE: str = "operational/shared/system-prompt.md"


# ---------------------------------------------------------------------------
# Pinned-binary seat (PINNED-CC.md). version is fixed at 2.1.152; the hash is a
# v1 placeholder (the binary is gitignored / not yet captured) but the SEAT must
# exist so the chokepoint's verify_binary(version=..., hash=...) has somewhere to
# read from (IMPLEMENTATION-PLAN §2.11 / §1 config row).
# ---------------------------------------------------------------------------

PINNED_BINARY_VERSION: str = "2.1.152"
# v1 placeholder — the pinned binary hash is not yet captured (the install is
# gitignored, PINNED-CC §"What's pinned"). Commissioning fills this in; the seat
# exists now so nothing downstream hardcodes the pinned hash at the spawn site.
PINNED_BINARY_HASH: str | None = None  # TODO(FORK / commissioning): capture sha256 of the pinned claude binary.


@dataclass(frozen=True)
class PinnedBinary:
    """The pinned-binary descriptor (PINNED-CC). `verify_binary` reads version+hash."""

    version: str = PINNED_BINARY_VERSION
    hash: str | None = PINNED_BINARY_HASH


PINNED_BINARY: PinnedBinary = PinnedBinary()


# ---------------------------------------------------------------------------
# The spec-model -> Claude-Code --model flag mapping (the transport increment).
#
# PROBED LIVE on the retained CC v2.1.152 (2026-07-24): `--model claude-opus-5` completes a real
# turn; the init row, assistant message.model, and result modelUsage key all name
# `claude-opus-5`. CC 2.1.152's result schema predates the richer canonicalModel/provider fields
# added by the official model-aware 2.1.219 release, but assistant message.model is the server
# response attribution surface this harness records. With NO --model, CC chooses its own default,
# so the mapping is EXPLICIT. CC does not validate arbitrary model ids at boot; an unmapped model
# adds NO flag (never guess an id). model_used remains the recorded INTENT; the E32
# configured-vs-actual fact-checker is deferred F17 territory.
# ---------------------------------------------------------------------------

CC_MODEL_FLAGS: dict[str, str] = {
    "opus-5.0": "claude-opus-5",  # exact released id — probed through a real turn on pinned 2.1.152
    # (The 2026-07-13 "gpt-5.6-sol" CLIProxyAPI entry was REMOVED 2026-07-16 with the whole Sol
    # proxy wiring — owner ruling: L5 runs the native Codex path, no CLIProxy. See the L5 row in
    # LEVEL_CONFIGS and design/working-notes/L5-CODEX-PATH-RULING-2026-07-16.md.)
}


# ---------------------------------------------------------------------------
# W(state) suspicion-window placeholder constants (FORK-W / WATCHDOG §3.3, §8).
#
# A renewal is overdue when `now - last_progress_at > W(state)`. The numbers are
# KNOWN-OPEN; v1 ships placeholders as config seats (NOT hardcoded inline) so
# commissioning can tune them without a code change.
# ---------------------------------------------------------------------------

# SUSPICION_WINDOWS is the SINGLE SOURCE (state -> seconds). The W_* module
# constants below are DERIVED from it, so a window added/tuned at commissioning is
# a one-line edit here, not two hand-synced places (the key set + a constant).
SUSPICION_WINDOWS: dict[str, int] = {
    "working": 120,           # seconds — actively producing output
    "waiting_on_child": 600,  # seconds — parent parked on a child (tolerates longer)
    "writing_final": 60,      # seconds — wrapping up the final report
}

W_working: int = SUSPICION_WINDOWS["working"]
W_waiting_on_child: int = SUSPICION_WINDOWS["waiting_on_child"]
W_writing_final: int = SUSPICION_WINDOWS["writing_final"]


def W(state: str) -> int:
    """Return the suspicion window (seconds) for a given runtime state.

    Placeholder values per FORK-W; commissioning tunes them. Raises KeyError for
    an unknown state so a typo at a call site fails loud rather than silently
    returning a wrong window.
    """
    return SUSPICION_WINDOWS[state]


# ---------------------------------------------------------------------------
# LevelConfig — the per-level seat carrying the five config-time dimensions the
# spawn machinery reads (model / runtime / reasoning_effort / role_variant / tool_manifest) plus the
# CONSTANT shared system-prompt base (carried here for convenience; identical
# across all levels) and a reference to the pinned binary.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LevelConfig:
    """Config-time seat for one level (L1..L5).

    Per runtime-and-model-map E31: model + runtime is a per-level, config-time,
    swappable dimension — an agent never picks its own. `role_variant` is the
    per-seat selector the chokepoint resolves to a role/load-manifest bundle.
    `system_prompt_file` is the runtime-global shared base path, identical across levels.
    """

    level: str
    model: str
    runtime: str
    role_variant: str
    tool_manifest: tuple[str, ...]
    # T-23: safe floor for sparse/ad-hoc configs. Production registry rows below always spell
    # their owner-calibrated value explicitly, so no seat falls to a runtime's low default.
    reasoning_effort: str = "high"
    # The shared base prompt — CONSTANT, identical across L1..L5.
    system_prompt_file: str = SYSTEM_PROMPT_FILE
    pinned_binary: PinnedBinary = field(default_factory=lambda: PINNED_BINARY)
    # SUPERVISED-SMOKE OVERRIDE (user-approved 2026-06-10; see
    # `unjailed_skip_permissions_requested` below): when True, an UNJAILED spawn adds
    # --dangerously-skip-permissions — explicitly decoupling SECURITY.md constraint 4's
    # skip-perms<->jail coupling for the small supervised smoke run. Default False: absent
    # the explicit opt-in, behavior is byte-identical to before the knob existed. NEVER set
    # in the LEVEL_CONFIGS registry — only the launch-path assemblers
    # (commissioning.build_runtime / get_level_config) stamp it from the env knob.
    unjailed_skip_permissions: bool = False
    # Strong-form local-file blinders are a PRODUCTION launch-path decision.  The registry/pure
    # accessor stays neutral for structural tests; commissioning.build_runtime and get_level_config
    # stamp the resolved observe/enforce mode.  There is deliberately no production "off" mode.
    blinders_mode: str | None = None

    @classmethod
    def for_level(cls, level: str) -> "LevelConfig":
        """Resolve the LevelConfig for an L1..L5 token (the factory accessor)."""
        try:
            return LEVEL_CONFIGS[level]
        except KeyError as exc:
            raise KeyError(f"unknown level {level!r}; known levels: {sorted(LEVEL_CONFIGS)}") from exc


# ---------------------------------------------------------------------------
# The per-level registry (runtime-and-model-map E32 assignment table).
#
# L1–L4 (and every + review seat) are Opus 5.0 on the Claude Code runtime
# (generative / architecture / planning / review seats). L5 is GPT-5.5 on the
# NATIVE Codex runtime (owner ruling 2026-07-16: the L5 Codex path, no CLIProxy,
# a SINGLE WARM RUNNER — see design/working-notes/L5-CODEX-PATH-RULING-2026-07-16.md;
# this reverses the runtime dimension of the 2026-07-13 unification ruling while
# keeping its seat-lifecycle unification: L5 remains a standing, daemon-spawned,
# watchdog-covered seat with its own transcript). Judgment diversity lives in
# BOTH the model and the runtime (L5 = GPT-5.5/Codex, L5+ = Opus/CC). At most ONE
# codex-runtime seat is active at a time — the chokepoint's single-runner
# admission gate defers further codex spawns until the runner frees.
# ---------------------------------------------------------------------------

# Coarse v1 placeholder tool surfaces, keyed by runtime. The real per-seat
# manifest is assembled by the chokepoint from the role_variant (ROLE-RESOLUTION
# §4); these are config seats so nothing downstream hardcodes a tool list.
_CLAUDE_CODE_TOOLS: tuple[str, ...] = ("read", "write", "edit", "bash", "task")
_CODEX_TOOLS: tuple[str, ...] = ("read", "write", "edit", "bash")

LEVEL_CONFIGS: dict[str, LevelConfig] = {
    "L1": LevelConfig(
        level="L1",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L1",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="xhigh",
    ),
    "L2": LevelConfig(
        level="L2",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L2",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="xhigh",
    ),
    "L3": LevelConfig(
        level="L3",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L3",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="xhigh",
    ),
    "L4": LevelConfig(
        level="L4",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L4",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
    # L5 CODEX PATH (owner ruling 2026-07-16; see
    # design/working-notes/L5-CODEX-PATH-RULING-2026-07-16.md): L5 runs GPT-5.6 Sol on the NATIVE
    # Codex runtime — no CLIProxy, no proxy env vars, no Keychain read. (Model corrected
    # gpt-5.5 -> gpt-5.6-sol by owner 2026-07-17: the 07-12 executor-model ruling — GPT-5.6 Sol —
    # survives the runtime reversal; "sol" is the real account model id, not a proxy alias.) This reverses the runtime
    # dimension of the 2026-07-13 unification ruling (whose Sol/CLIProxyAPI wiring was removed the
    # same day) while KEEPING its lifecycle unification: L5 is still a standing, daemon-spawned,
    # watchdog-covered seat with its own rollout transcript (the CodexAdapter's E32
    # pin->gate->open->record recipe; probes re-verified live on codex-cli 0.144.1, 2026-07-16).
    # Concurrency: a SINGLE WARM RUNNER — the chokepoint admits at most one active codex-runtime
    # seat; further codex spawns defer (binding stays planned; the daemon re-drive sweep retries)
    # until the runner frees. This makes the CODEX-CONCURRENT-AUTH refresh race unreachable by
    # construction (one runner, ephemeral seat home, canonical home owns refresh).
    "L5": LevelConfig(
        level="L5",
        model="gpt-5.6-sol",
        runtime="codex",
        role_variant="L5#exec",
        tool_manifest=_CODEX_TOOLS,
        reasoning_effort="high",
    ),
    # L5+ — the independent per-unit reviewer (QUALITY-GATE M52): Opus 5.0 on Claude Code,
    # DELIBERATELY a different model AND runtime from the GPT-5.5/Codex L5 it reviews (judgment
    # diversity — "two models sharing fewer correlated failure modes means the review catches
    # more", L5 role doc).
    #
    # L4+/L3+/L2+ — higher composition review seats. Their runtime/model assignment remains a
    # commissioning knob (HIGHER-LEVEL-GATES.md); this registry gives the current spawn machinery a
    # concrete role_variant and manifest target so a review seat never opens with the producer's
    # executor docs by accident.
    "L2+": LevelConfig(
        level="L2+",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L2+#review",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
    "L3+": LevelConfig(
        level="L3+",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L3+#review",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
    "L4+": LevelConfig(
        level="L4+",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L4+#review",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
    "L5+": LevelConfig(
        level="L5+",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="L5+#review",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
}

# Q4/Q5 owner+director calibration, applied 2026-07-24. Literal/driving seats use GPT-5.6 Sol;
# judgment seats use Opus 5.0. The calibration session's four Sol/Claude-Code runtime cells were
# reconciled by explicit director ruling to the proven native Codex adapter: the removed proxy path
# cannot serve Sol through Claude Code, and a config-only Claude row would falsely record Sol while
# launching Claude's default. Lifecycle remains the same standing-seat substrate.
SEMANTIC_CELL_LEVEL_CONFIGS: dict[str, LevelConfig] = {
    "reconstruction-verification": LevelConfig(
        level="L2+",
        model="gpt-5.6-sol",
        runtime="codex",
        role_variant="plan-alignment#reconstruction-verification",
        tool_manifest=_CODEX_TOOLS,
        reasoning_effort="high",
    ),
    "reconstruction-construction": LevelConfig(
        level="L2+",
        model="gpt-5.6-sol",
        runtime="codex",
        role_variant="plan-alignment#reconstruction-construction",
        tool_manifest=_CODEX_TOOLS,
        reasoning_effort="high",
    ),
    "comparator": LevelConfig(
        level="L2+",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="plan-alignment#comparator",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
    "coherence": LevelConfig(
        level="L2+",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="plan-alignment#coherence",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
    "atomization": LevelConfig(
        level="L2+",
        model="opus-5.0",
        runtime="claude-code",
        role_variant="plan-alignment#atomization",
        tool_manifest=_CLAUDE_CODE_TOOLS,
        reasoning_effort="high",
    ),
}

PRODUCT_PROBE_LEVEL_CONFIGS: dict[str, LevelConfig] = {
    "user-simulation": LevelConfig(
        level="L2+",
        model="gpt-5.6-sol",
        runtime="codex",
        role_variant="product-probe#user-simulation",
        tool_manifest=_CODEX_TOOLS,
        reasoning_effort="high",
    ),
    "performance-robustness": LevelConfig(
        level="L2+",
        model="gpt-5.6-sol",
        runtime="codex",
        role_variant="product-probe#performance-robustness",
        tool_manifest=_CODEX_TOOLS,
        reasoning_effort="high",
    ),
}


# ---------------------------------------------------------------------------
# CODEX_MODEL_FLAGS — the CC_MODEL_FLAGS analog for the Codex runtime (E4). The
# value is the `-m/--model` id handed to the pinned codex CLI. PROBED LIVE
# (0.128.0, ChatGPT account, 2026-06-11; RE-PROBED on 0.144.1, 2026-07-16):
# `-m gpt-5.5` is accepted AND the rollout records "model":"gpt-5.5" as fact
# (turn_context row on 0.144.1). Explicit mapping — an unmapped model adds NO
# flag (never guess an id).
# ---------------------------------------------------------------------------

CODEX_MODEL_FLAGS: dict[str, str] = {
    "gpt-5.5": "gpt-5.5",  # probed: accepted + recorded as fact in the rollout
    # gpt-5.6-sol — GPT-5.6 Sol on the NATIVE Codex/ChatGPT path (owner correction 2026-07-17:
    # the 07-12 executor-model ruling — GPT-5.6 Sol — survives the runtime reversal; "sol" is
    # the real model id on the account, NOT a CLIProxy alias). PROBED LIVE on the pinned
    # codex-cli 0.144.1 (2026-07-17): `-m gpt-5.6-sol` exec turn succeeds and the rollout
    # records "model":"gpt-5.6-sol" as fact. (Bare `-m gpt-5.6` is REJECTED by the server on
    # a ChatGPT account — probed the same morning.)
    "gpt-5.6-sol": "gpt-5.6-sol",
}

# The pinned Codex CLI (the .cc-pinned precedent): npm @openai/codex pinned in
# .codex-pinned/, with its isolated CODEX_HOME at .codex-pinned/config. Its
# auth.json is the one-lineage symlink to ~/.codex/auth.json; its other config
# remains isolated (no global AGENTS.md, no user hooks/MCP).
# PIN BUMP 0.128.0 -> 0.144.1 (2026-07-16): the 0.128.0 native binary is SIGKILLed on
# this OS (Darwin 25.5) at exec, from any path, in and out of sandbox — unbootable.
# 0.144.1 (the machine's proven live version) re-probed clean: rollout header shape
# (session_meta payload.id/cwd) unchanged, `-m gpt-5.5` accepted + recorded as fact
# in turn_context, exec turns succeed on the pinned home.
PINNED_CODEX_VERSION: str = "0.144.1"
PINNED_CODEX_BINARY: str = ".codex-pinned/node_modules/.bin/codex"
PINNED_CODEX_HOME: str = ".codex-pinned/config"


# ---------------------------------------------------------------------------
# SUPERVISED-SMOKE OVERRIDE — legacy-named explicit runtime permission knob.
#
# SECURITY.md constraint 4 couples skip-permissions to the jail ("skip-permissions INSIDE
# the jail … containment bounds the blast radius"), so the adapter adds
# --dangerously-skip-permissions ONLY when a containment block is resolved. The FIRST
# supervised live run is a small UNJAILED smoke run; the user explicitly decided
# (2026-06-10): "Unjailed + dangerously skip permissions. It is a small run, the risk of
# something catastrophic happening is minimal." This knob is that decision as an explicit,
# loud, opt-in seam — never a silent decoupling:
#
#   * opt-in is STRICTLY HARNESS_UNJAILED_SKIP_PERMISSIONS=1 (no fuzzy truthiness);
#   * the env var is read at the LAUNCH-PATH ASSEMBLERS (commissioning.build_runtime for
#     the genesis L1; get_level_config for the ipc/outbox child-spawn resolution) — NEVER
#     inside the adapter (the adapter reads only the explicit LevelConfig field);
#   * the posture is journaled (SpawnResult.permission_posture +/ the STEP4 binding stamp
#     `permission_posture: unjailed-skip-permissions-override`, SECURITY.md §4.3);
#   * production now always carries blinders observe/enforce. The legacy field/env name remains
#     for compatibility, but it no longer implies an unjailed production spawn.
# ---------------------------------------------------------------------------

UNJAILED_SKIP_PERMISSIONS_ENV: str = "HARNESS_UNJAILED_SKIP_PERMISSIONS"
BLINDERS_MODE_ENV: str = "HARNESS_BLINDERS_MODE"


def production_blinders_mode(environ=None) -> str:
    """Resolve the production read-policy mode (default observe; explicit enforce).

    Invalid values fail before spawn instead of silently disabling the physical policy.
    """
    env = os.environ if environ is None else environ
    value = str(env.get(BLINDERS_MODE_ENV, "observe")).strip().lower()
    if value not in {"observe", "enforce"}:
        raise ValueError(
            f"{BLINDERS_MODE_ENV} must be 'observe' or 'enforce', got {value!r}"
        )
    return value


def unjailed_skip_permissions_requested(environ=None) -> bool:
    """True iff the operator EXPLICITLY set HARNESS_UNJAILED_SKIP_PERMISSIONS=1 (strictly "1").

    The single read seam for the supervised-smoke override (see the block comment above).
    Called only by the launch-path assemblers; the adapter never calls this.
    """
    env = os.environ if environ is None else environ
    return env.get(UNJAILED_SKIP_PERMISSIONS_ENV) == "1"


def get_level_config(level: str) -> LevelConfig:
    """Module-level accessor: resolve the LevelConfig for an L1..L5 token.

    THIS is the launch-path resolver the daemon's child-spawn paths use (ipc.spawn /
    outbox.service — the L2..L5 children of the L1->L5 smoke run), so it applies the
    SUPERVISED-SMOKE OVERRIDE here, mirroring commissioning.build_runtime for genesis.
    The override lands on a per-call COPY (`dataclasses.replace`) — the shared
    LEVEL_CONFIGS singletons and the pure `LevelConfig.for_level` accessor stay pristine.
    """
    lc = replace(
        LevelConfig.for_level(level),
        blinders_mode=production_blinders_mode(),
    )
    if unjailed_skip_permissions_requested():
        lc = replace(lc, unjailed_skip_permissions=True)
    return lc
