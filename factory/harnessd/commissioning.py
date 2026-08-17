"""commissioning — assemble the runtime descriptor that ``daemon.run`` boots (the live-run gate).

The substrate is built + unit-tested; ``daemon.run``/``boot`` READ ``ledger.RUNTIME_ROOT``, the dedicated
tmux-server socket, and the spawn adapter — but nothing ASSEMBLED them for a real launch. This module is
that assembler: it wires the REAL ``ClaudeCodeAdapter`` + the 4-var OAuth-only floor and
non-credential PATH (read from the pinned install) + the L1 root config into the ``runtime``
descriptor ``boot``/``run_genesis`` consume.

OAUTH-ONLY (HARD invariant): the env carries ``CLAUDE_CODE_OAUTH_TOKEN`` and NEVER a raw
``ANTHROPIC_API_KEY``/``OPENAI_API_KEY`` (the pane's ``env -i`` + oauth_guard enforce it at spawn; this
assembler simply never puts a raw key in).

This module is intentionally separate from ``config`` (config is import-cycle-free; constructing the
adapter here would cycle config<->adapter). It is imported only by the daemon's ``__main__`` launch path.
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from types import SimpleNamespace

from harnessd import config, ledger, review_dispatch
from harnessd import executor as _executor
from harnessd.spawn import tmux as _tmux

# The pinned, isolated Claude Code install (v2.1.152) + its clean CLAUDE_CONFIG_DIR (no inherited hooks/
# MCP/patches) — the OAuth-only credential lives at <config>/.oauth_token (DAEMON §7; CLAUDE.md).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PINNED_CONFIG_DIR = _REPO_ROOT / ".cc-pinned" / "config"
PRODUCTION_PANE_PATH = (
    "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
)

# Workspaces live OUTSIDE the repo, in their own root (user ruling 2026-06-12): the repo is
# code + spec; the trees the harness builds are deployment state, not repo contents. Relocated
# out of ~/Documents (user ruling 2026-07-07): every ancestor of a seat's cwd must stay free of
# ambient agent-instruction files — Claude Code auto-loads CLAUDE.md from all ancestor dirs, and
# ~/Documents now carries one. Per-run $HARNESS_RUNTIME_ROOT still wins; $HARNESS_WORKSPACES_ROOT
# relocates the whole family.
DEFAULT_WORKSPACES_ROOT = Path.home() / "l1-l5-workspaces"

# The L1 root address/level (the parentless System Orchestrator — genesis registers it parentless, §7).
L1_ADDRESS = "L1#exec"
L1_LEVEL = "L1"
INITIAL_INTAKE_ENV = "HARNESS_L1_INTAKE"
FIDELITY_PLAYBACK_AUTHORITY_ENV = "HARNESS_FIDELITY_PLAYBACK_AUTHORITY"
FIDELITY_PLAYBACK_DELEGATE_ENV = "HARNESS_FIDELITY_PLAYBACK_DELEGATE"
FIDELITY_PLAYBACK_DELEGATION_REASON_ENV = (
    "HARNESS_FIDELITY_PLAYBACK_DELEGATION_REASON"
)


def normalize_review_panel_arms(declarations=None) -> list[dict]:
    """Normalize ordered ``pattern=axis,...`` module-panel declarations."""
    accepted = review_dispatch.CONFIGURABLE_MODULE_REVIEW_AXES
    accepted_text = ", ".join(accepted)
    normalized: list[dict] = []
    for declaration in declarations or []:
        if isinstance(declaration, dict):
            pattern = str(declaration.get("pattern") or "").strip()
            raw_axes = declaration.get("axes") or []
            axes = [str(axis).strip() for axis in raw_axes]
        else:
            raw = str(declaration or "")
            if "=" not in raw:
                raise RuntimeError(
                    "review panel arm must be <module-address-glob>=<axis>[,<axis>...]; "
                    f"accepted axes: {accepted_text}"
                )
            pattern, raw_axis_text = raw.split("=", 1)
            pattern = pattern.strip()
            axes = [axis.strip() for axis in raw_axis_text.split(",")]
        if (
            not pattern
            or not axes
            or any(not axis for axis in axes)
            or any(axis not in accepted for axis in axes)
            or len(set(axes)) != len(axes)
        ):
            raise RuntimeError(
                "invalid review panel arm; require a non-empty module address pattern and "
                "a duplicate-free ordered axis list; "
                f"accepted axes: {accepted_text}"
            )
        normalized.append({"pattern": pattern, "axes": axes})
    return normalized


def _fidelity_playback_authority(environ=None) -> tuple[str, str | None, str | None]:
    """Resolve the explicit run-scoped owner/delegate fidelity-playback authority."""
    source = os.environ if environ is None else environ
    return validate_fidelity_playback_authority(
        source.get(FIDELITY_PLAYBACK_AUTHORITY_ENV),
        source.get(FIDELITY_PLAYBACK_DELEGATE_ENV),
        source.get(FIDELITY_PLAYBACK_DELEGATION_REASON_ENV),
    )


def validate_fidelity_playback_authority(
    authority,
    delegate,
    delegation_reason,
) -> tuple[str, str | None, str | None]:
    """Normalize and validate one explicit owner/delegate declaration triple."""
    authority = str(authority or "owner").strip().lower()
    delegate = str(delegate or "").strip()
    reason = str(delegation_reason or "").strip()
    if authority not in {"owner", "operator-delegate"}:
        raise RuntimeError(
            f"{FIDELITY_PLAYBACK_AUTHORITY_ENV} must be owner or operator-delegate"
        )
    if authority == "operator-delegate":
        if not delegate or not reason:
            raise RuntimeError(
                "operator-delegate fidelity playback authority requires both "
                f"{FIDELITY_PLAYBACK_DELEGATE_ENV} and "
                f"{FIDELITY_PLAYBACK_DELEGATION_REASON_ENV}"
            )
    elif delegate or reason:
        raise RuntimeError(
            "fidelity playback delegate/reason inputs require "
            f"{FIDELITY_PLAYBACK_AUTHORITY_ENV}=operator-delegate"
        )
    return authority, delegate or None, reason or None


def _pinned_token_file() -> Path:
    """The pinned install's OAuth token file (a test seam patches this to avoid the live token)."""
    return _PINNED_CONFIG_DIR / ".oauth_token"


def _read_oauth_token() -> str:
    """Read the OAuth subscription token: $CLAUDE_CODE_OAUTH_TOKEN, else the pinned .oauth_token file.

    Raises a clear commissioning error if absent (genesis would otherwise fail the credential
    precondition — a token lapse now reads as 'refresh the token', F3/FORK-TOKEN-EXPIRY)."""
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_tok:
        return env_tok
    tok_file = _pinned_token_file()
    if tok_file.is_file():
        tok = tok_file.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    raise RuntimeError(
        "no OAuth token: set $CLAUDE_CODE_OAUTH_TOKEN or write the pinned install's token to "
        f"{_pinned_token_file()} (refresh it via the pinned-install login). The harness is OAuth-only — "
        "a raw API key is forbidden."
    )


def tmux_socket_name(build_id: str) -> str:
    """The dedicated tmux SERVER socket name (``tmux -L <name>``) for this daemon — isolated from the
    user's default tmux server. A short, memorable name so the operator can attach to watch the panes:
    ``tmux -L <name> attach -t harness:<addr>`` (visible-mode, task #11)."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(build_id).strip()).strip("-").lower()
    return f"harnessd-{slug or 'build'}"


_tmux_socket_name = tmux_socket_name  # historical private name retained for existing callers/tests


def resolve_runtime_identity(*, runtime_root=None, build_id: str = None) -> tuple[Path, str]:
    """Resolve the build id + canonical runtime root without touching OAuth/runtime assembly."""
    resolved_build_id = build_id or os.environ.get("HARNESS_BUILD_ID") or "build-local"
    if runtime_root is None:
        env_root = os.environ.get("HARNESS_RUNTIME_ROOT")
        if env_root:
            runtime_root = Path(env_root)
        else:
            ws_env = os.environ.get("HARNESS_WORKSPACES_ROOT")
            base = Path(ws_env) if ws_env else DEFAULT_WORKSPACES_ROOT
            runtime_root = base / resolved_build_id
    return Path(runtime_root).expanduser().resolve(), str(resolved_build_id)


def build_runtime(
    *,
    runtime_root=None,
    build_id: str = None,
    oauth_token: str = None,
    initial_intake: str = None,
    fidelity_playback_authority: str = None,
    fidelity_playback_delegate: str = None,
    fidelity_playback_delegation_reason: str = None,
    review_panel_arms=None,
) -> SimpleNamespace:
    """Assemble the ``runtime`` descriptor ``daemon.run``/``boot`` consume.

    runtime_root: where the per-build tree lives; resolved from the arg, else $HARNESS_RUNTIME_ROOT,
    else ``$HARNESS_WORKSPACES_ROOT/<build-id>``, else ``DEFAULT_WORKSPACES_ROOT/<build-id>``
    (~/l1-l5-workspaces — outside the repo AND outside ~/Documents; user rulings 2026-06-12 +
    2026-07-07). build_id: the arg,
    else $HARNESS_BUILD_ID, else ``build-local``. oauth_token: the arg, else the pinned install.
    initial_intake: the arg, else ``$HARNESS_L1_INTAKE``; when present, genesis pre-seeds it into
    the L1 inbox before the root actor opens. review_panel_arms: an optional ordered list of
    normalized L3 module-pattern panel declarations; a non-empty list is carried by the genesis
    config to the parentless run root.

    Returns a descriptor carrying: ``runtime_root``, ``build_id``, ``config`` (the genesis cfg: env +
    l1_address/level + runtime_root + build_id + pinned_binary + level_config), ``adapter`` (the REAL
    ClaudeCodeAdapter), ``executor`` + ``tmux`` (the genesis collaborators), and ``tmux_socket`` (the
    dedicated server name for attach). The global seams (ledger.RUNTIME_ROOT + tmux socket) are bound by
    ``daemon._apply_global_seams`` at run() time, NOT here (this is pure construction).
    """
    runtime_root, build_id = resolve_runtime_identity(
        runtime_root=runtime_root,
        build_id=build_id,
    )

    token = oauth_token or _read_oauth_token()
    if initial_intake is None:
        initial_intake = os.environ.get(INITIAL_INTAKE_ENV)
    if any(
        value is not None
        for value in (
            fidelity_playback_authority,
            fidelity_playback_delegate,
            fidelity_playback_delegation_reason,
        )
    ):
        (
            fidelity_playback_authority,
            fidelity_playback_delegate,
            fidelity_playback_delegation_reason,
        ) = validate_fidelity_playback_authority(
            fidelity_playback_authority,
            fidelity_playback_delegate,
            fidelity_playback_delegation_reason,
        )
    else:
        (
            fidelity_playback_authority,
            fidelity_playback_delegate,
            fidelity_playback_delegation_reason,
        ) = _fidelity_playback_authority()
    review_panel_arms = normalize_review_panel_arms(review_panel_arms)

    # The OAuth-only isolation env (DAEMON §6.2) + PATH (LR-2, user posture decision 2026-06-11:
    # PoC-phase security flips allowlist->denylist; the 4-var floor's missing PATH made every
    # agent subshell fail-and-rediscover `python3`/`head` every turn — pure friction, zero
    # security value: PATH is not a credential). The credential invariant is UNCHANGED: the
    # OAuth token + pinned config dir, never a raw API key.
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "CLAUDE_CONFIG_DIR": str(_PINNED_CONFIG_DIR),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "PATH": PRODUCTION_PANE_PATH,
    }

    # SUPERVISED-SMOKE OVERRIDE (user-approved 2026-06-10): when the operator launched this
    # daemon with HARNESS_UNJAILED_SKIP_PERMISSIONS=1 (strictly "1" — config owns the read seam),
    # the genesis L1 LevelConfig carries unjailed_skip_permissions=True. The legacy name records
    # its supervised-smoke origin; production now also always stamps blinders observe/enforce, so
    # this changes the runtime permission flag inside an external jail rather than disabling the
    # filesystem boundary. Child spawns resolve the same posture via config.get_level_config.
    level_config = dataclasses.replace(
        config.LevelConfig.for_level(L1_LEVEL),
        blinders_mode=config.production_blinders_mode(),
    )
    if config.unjailed_skip_permissions_requested():
        level_config = dataclasses.replace(level_config, unjailed_skip_permissions=True)

    cfg = SimpleNamespace(
        env=env,
        l1_address=L1_ADDRESS,
        l1_level=L1_LEVEL,
        runtime_root=runtime_root,
        build_id=build_id,
        pinned_binary=config.PINNED_BINARY,
        level_config=level_config,
        initial_intake=initial_intake,
        fidelity_playback_authority=fidelity_playback_authority,
        fidelity_playback_delegate=fidelity_playback_delegate,
        fidelity_playback_delegation_reason=fidelity_playback_delegation_reason,
    )
    if review_panel_arms:
        cfg.review_panel_arms = review_panel_arms

    # E4: production ships adapter=None — the chokepoint resolves adapters from the PER-RUNTIME
    # REGISTRY (daemon._apply_global_seams registers claude-code + codex). Since the 2026-07-13
    # L5-runtime unification L5 resolves the ClaudeCodeAdapter like every other seat; the
    # CodexAdapter stays registered as dormant capability. Injecting a single adapter here would WIN
    # over the registry (the injected seam is the explicit test override) and recreate the LT-8/O1
    # silent divergence.
    return SimpleNamespace(
        runtime_root=runtime_root,
        build_id=build_id,
        config=cfg,
        adapter=None,
        executor=_executor,
        tmux=_tmux,
        tmux_socket=tmux_socket_name(build_id),
    )
