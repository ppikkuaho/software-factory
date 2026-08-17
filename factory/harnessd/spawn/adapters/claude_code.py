"""claude_code — the concrete Claude-Code RuntimeAdapter (IMPLEMENTATION-PLAN §2.11; DAEMON §6.2).

The FROZEN H40 boot recipe (the ONE Claude-Code spawn the whole harness uses):

  argv = [CC, "--system-prompt-file", system_prompt_file, "--effort", reasoning_effort]
      where ``system_prompt_file`` is the per-spawn COMPOSED IDENTITY BUNDLE
      (``_compose_identity_prompt``: the shared ``config.SYSTEM_PROMPT_FILE`` first, then the
      level's soul/role/config under provenance headers — AMENDED 2026-06-12, user ruling /
      LR-4 cure: identity acquisition never rides on agent diligence). The shared protocol
      docs still arrive as the brief's load-manifest (role-as-documents), which the agent
      READS in place. argv NEVER carries ``--bare`` (forces API-key auth, breaks the OAuth
      token), ``--append-system-prompt`` (keeps the full base framing), or
      ``--agents``/``--agent`` (does not inject the persona) — the H40 foot-guns.

  env  = exactly the 4 isolation vars {CLAUDE_CONFIG_DIR, CLAUDE_CODE_OAUTH_TOKEN,
         CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, DISABLE_AUTOUPDATER}. No raw API key.

  session = addressing.session_name_for(address)  ('harness-' + the address with '/', '#',
      ':', '.' all folded to '-' — F18: a name tmux 3.6a will NOT silently rename). The RECORDED
      tmux_target is the canonical '<session>:<window>.<pane>' triple create_detached RETURNS
      (tmux's own post-rename report) so reconcile/pane_alive match tmux<->ledger byte-for-byte.

  The pane handed to ``tmux.create_detached`` is the from-empty isolator ``env -i <K=V…> <argv…>``
  (``tmux.build_pane_argv`` — the SAME seam the wrapper uses), and the OAuth-only gate
  (``oauth_guard.assert_oauth_only(env, argv, pane_argv, tmux.server_env())``) fires BEFORE
  ``create_detached`` so NO actor opens on a forbidden env (E32 ordering). The Claude-specific
  POSITIVE token check (``check_credential_health``) runs in this path (NOT the shared gate).

Recorded facts (config = INTENT, model_used = FACT): model_used = "<model> / <runtime>" DERIVED
from the level_config (LT-8 — never a constant that can contradict the config; the deferred F17
configured-vs-actual fact-checker can only reconcile intent that is real), role_variant,
system_prompt_file (the composed identity bundle actually loaded) + its content hash,
session_uuid, and the derived transcript_path (a ``<session-uuid>.jsonl`` file the detector stats).

BUILDER DECISIONS for the §2.11 details the plan leaves open (stated in the build report):

  * verify_binary — verifies the pinned VERSION against ``config.PINNED_BINARY_VERSION`` without
    a subprocess (the dry-run forbids any real exec, and ``config.PINNED_BINARY_HASH`` is a v1
    placeholder = None, so a real ``claude --version`` / sha256 probe is the documented seam
    DEFERRED to commissioning — FORK-VERIFY). It is a pure, no-exec confirmation in v1.
  * deterministic first-boot trust — NOT a send-keys race against the trust dialog; it is the
    pre-seeded clean ``CLAUDE_CONFIG_DIR`` (.cc-pinned/config, pre-trusted, non-interactive) the
    env already points at. v1 carries this as a no-op marker (the config dir IS the mechanism).
  * transcript_path derivation — ``<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>/<uuid>.jsonl``:
    Claude Code files transcripts under ``<config>/projects/<encoded-cwd>/<session-uuid>.jsonl``,
    where <encoded-cwd> is the pane's REALPATH cwd with every non-[A-Za-z0-9-] char folded to '-'.
    The uuid is OURS: argv carries ``--session-id <uuid>`` so CC writes the EXACT file we record
    (first-live-run finding 2026-06-11: a session-NAME-derived segment + an un-pinned uuid pointed
    the detector at a file CC never writes; verify-new-turn read size-0 forever and the idle
    ladder failed a healthy waiting L1 with watchdog_nonresponse).
  * CC binary path — the pinned ``.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe``
    resolved relative to HARNESS_ROOT. Never execed in the dry-run (the no_real_exec spy proves it).
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from pathlib import Path

from harnessd import addressing, config, turn_state
from harnessd.spawn import oauth_guard, sandbox, cc_config

from .base import RuntimeAdapter, SpawnResult


# The pinned Claude binary, relative to HARNESS_ROOT (PINNED-CC.md). A path constant, never
# flattened with role text; never execed in the dry-run (mock tmux + the no_real_exec spy).
_PINNED_CC = ".cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe"

# The exact 4-var isolation set the env MUST equal (DAEMON §6.2). Defined here so a missing/extra
# var fails the assembly loudly rather than silently spawning a widened env.
_ISOLATION_ENV_KEYS = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
    }
)

# (The 2026-07-13 "wiring (ii)" Sol/CLIProxyAPI proxy-env machinery — _PROXY_ENV_KEYS,
# _fetch_sol_proxy_token, _resolve_proxy_env, the recorded-env token redaction — was REMOVED
# 2026-07-16 with the L5 Codex-path ruling: no claude-code seat is proxied anymore; every CC pane
# carries exactly the 4-var isolation floor. ANTHROPIC_AUTH_TOKEN is now refused on every pane
# (oauth_guard). See design/working-notes/L5-CODEX-PATH-RULING-2026-07-16.md.)

# Claude Code's native background-agent tool can perform useful local work, but it is not part of
# the harness supervision tree. Producer seats delegate product work through child nodes +
# `.harness-outbox`, where the daemon records bindings, gates, transcripts, and ownership.
_NATIVE_AGENT_TOOL = "Agent"

# The legacy recorded-intent FALLBACK — used only when a level_config lacks model/runtime seats
# (a sparse test fake). The real record derives from the level_config (LT-8, _model_used below).
_MODEL_USED = "opus-5.0 / claude-code"


def _model_used(level_config) -> str:
    """The recorded INTENT, derived from the CONFIG (LT-8): ``"<model> / <runtime>"``.

    The old constant recorded one Opus/Claude-Code value regardless of level_config — so an L5
    configured gpt-5.5/codex but driven through THIS adapter recorded an intent that contradicted
    its own config (and the actual unflagged-CC default), a three-way divergence the deferred F17
    fact-checker could not even reconstruct. Intent now comes from the config; the
    configured-vs-ACTUAL check remains F17 territory.
    """
    model = getattr(level_config, "model", None)
    runtime = getattr(level_config, "runtime", None)
    if model and runtime:
        return f"{model} / {runtime}"
    return _MODEL_USED


def _fence_native_agent_tool(level_config) -> bool:
    """Return True when a Claude producer seat must not see the native Agent tool.

    Review seats have their own dispatch-design surface. L1 also has a canonical non-product use
    of native Agent: the throwaway intake/research session that returns an intent-spec or evidence
    artifact to L1. The live LR-48 failure was a lower producer coordinator using native Agent as
    if it were a harness child; fence that path where it can bypass bindings, gates, and
    parent-visible evidence.
    """
    role_variant = str(getattr(level_config, "role_variant", "") or "")
    level = str(getattr(level_config, "level", "") or "").split("#", 1)[0]
    if "#review" in role_variant or level.endswith("+"):
        return False
    return level in {"L2", "L3", "L4"}


def _harness_root() -> Path:
    """Resolve HARNESS_ROOT — the repo root the relative config/pinned paths join against.

    The daemon is launchd-managed with a CWD that is NOT guaranteed to be the repo root
    (config.py NOTE), so paths are joined against the resolved root, never passed raw to a
    launchd-CWD process. This module lives at ``<root>/harnessd/spawn/adapters/claude_code.py``,
    so the root is three parents up.
    """
    return Path(__file__).resolve().parents[3]


def _system_prompt_hash() -> str:
    """sha256 of the SHARED system-prompt base content.

    Reads the constant ``config.SYSTEM_PROMPT_FILE`` under HARNESS_ROOT. If the file is not on
    disk (a CI without the operational tree), fall back to hashing the PATH string so the
    value is still non-empty and deterministic. Claude seats now record the composed identity
    bundle hash from ``_compose_identity_prompt`` as the spawn fact; this helper is kept as a
    base-prompt utility for older callers/tests.
    """
    spf = config.SYSTEM_PROMPT_FILE
    path = _harness_root() / spf
    try:
        data = path.read_bytes()
    except OSError:
        data = spf.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _compose_identity_prompt(level_config, workspace, launch_packet_file=None) -> tuple:
    """Compose the per-spawn IDENTITY system prompt: shared prompt + the level's soul/role/config.

    LR-4 cure (user ruling 2026-06-12, amending H40 Decision B): identity acquisition must not
    ride on agent diligence — the trio is FLATTENED into the system prompt CC loads before its
    first token. The shared prompt (``config.SYSTEM_PROMPT_FILE``) leads; each doc follows under
    a provenance header; a trailer points at the brief's load-manifest for the read-in-place
    protocol docs (those stay a reference library, not identity). Written to
    ``<workspace>/.identity-prompt.md`` (the agent may re-read it; the file is jail-readable in
    its own node) — or a temp file when no workspace rides the brief (bare adapter dry-runs).
    Returns ``(path, sha256_of_content)``. A missing identity doc raises SpawnFailure
    (failure_class='identity_missing') — E1's pieces gate makes that unreachable in production;
    this is the fail-loud net, never a silent fallback to the bare shared prompt.
    """
    level = (getattr(level_config, "level", None) or "").strip() or "L5"
    root = _harness_root()
    parts = []
    try:
        parts.append((root / config.SYSTEM_PROMPT_FILE).read_text(encoding="utf-8"))
    except OSError as exc:
        failure = oauth_guard.SpawnFailure(f"identity compose: shared system prompt unreadable ({exc})")
        failure.failure_class = "identity_missing"
        raise failure
    if launch_packet_file:
        launch_path = Path(str(launch_packet_file))
        try:
            launch_body = launch_path.read_text(encoding="utf-8")
        except OSError as exc:
            failure = oauth_guard.SpawnFailure(
                f"identity compose: launch packet {launch_path} unreadable ({exc})"
            )
            failure.failure_class = "identity_missing"
            raise failure
        parts.append(
            "\n\n---\n\n"
            f"<!-- launch-packet: {launch_path} (auto-loaded at spawn) -->\n\n"
            f"{launch_body}"
        )
        parts.append(
            "\n\n---\n\n"
            "The launch packet above is your startup surface. Use `.reference-map.md` in your "
            "workspace only when the task gives you a concrete reason to look something up.\n"
        )
        content = "".join(parts)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return _write_identity_prompt(content, workspace, digest)

    for rel in _brief_module().identity_docs(level):
        try:
            body = (root / rel).read_text(encoding="utf-8")
        except OSError as exc:
            failure = oauth_guard.SpawnFailure(
                f"identity compose: {rel} unreadable ({exc}) — an under-equipped actor never opens"
            )
            failure.failure_class = "identity_missing"
            raise failure
        parts.append(f"\n\n---\n\n<!-- identity: {rel} (auto-loaded at spawn — LR-4) -->\n\n{body}")
    trailer = (
        "\n\n---\n\nThe remaining protocol documents are listed in your brief's load-manifest "
        "(\"Identity \u2014 Load These Documents\") \u2014 read them in place before starting work.\n"
    )
    if _fence_native_agent_tool(level_config):
        trailer += (
            "\nFor delegated child work, prepare the child node and write a one-line "
            "`.harness-outbox/<seq>-<child>.json` request with `child_name` and `child_level`; "
            "the harness daemon opens the child. A runtime-native `Agent` is not a harness child "
            "and is not a product-work delegation path. If you need lower-level work, follow "
            "`operational/shared/agent-lifecycle.md` -> `How You Spawn a Child`.\n"
        )
    elif level == "L1":
        trailer += (
            "\nAs L1, runtime-native `Agent` is available for the L1-owned throwaway intake "
            "grilling session and evidence-gathering research described in your role docs. It "
            "returns an intent-spec or evidence artifact to you; it is not a project/product "
            "child. Project work still starts by preparing the child node and writing a one-line "
            "`.harness-outbox/<seq>-<child>.json` request with `child_name` and `child_level`, so "
            "the harness daemon opens L2/L3/L4/L5 work under bindings, gates, and transcripts.\n"
        )
    parts.append(trailer)
    content = "".join(parts)

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return _write_identity_prompt(content, workspace, digest)


def _write_identity_prompt(content: str, workspace, digest: str) -> tuple:
    import tempfile

    if workspace:
        target_dir = Path(str(workspace))
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / ".identity-prompt.md"
            path.write_text(content, encoding="utf-8")
            return path, digest
        except OSError:
            pass  # an unwritable workspace must not cost the agent its identity — temp fallback
    fd, tmp = tempfile.mkstemp(suffix=".identity-prompt.md", prefix="cc-")
    import os as _os
    _os.close(fd)
    path = Path(tmp)
    path.write_text(content, encoding="utf-8")
    return path, digest


def _brief_module():
    """Lazy import (brief.py is dependency-free; lazy keeps adapter import order unchanged)."""
    from harnessd.spawn import brief as _brief

    return _brief


def _transcript_path(env: dict, cwd: str | None, session_uuid: str) -> str:
    """Derive ``<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>/<session_uuid>.jsonl``.

    Claude Code files transcripts by the pane's REALPATH cwd — every char outside [A-Za-z0-9-]
    folded to '-' (probed on pinned 2.1.152: '/', '.', '_' all fold; case preserved; the leading
    '/' yields the leading '-'; macOS /var/... realpaths to /private/var/...). The session_uuid
    must be pinned into CC's argv via ``--session-id`` by the caller — only then is this path the
    file CC actually writes. The detector/watchdog stat this path for verify-new-turn; a wrong
    path here reads size-0 forever and the idle ladder kills healthy agents (2026-06-11 live run).
    A None cwd means the pane inherits the daemon's cwd (no ``-c``), so encode that.
    """
    config_dir = env.get("CLAUDE_CONFIG_DIR", "")
    real = os.path.realpath(cwd) if cwd else os.path.realpath(os.getcwd())
    project_seg = re.sub(r"[^A-Za-z0-9-]", "-", real)
    return str(Path(config_dir) / "projects" / project_seg / f"{session_uuid}.jsonl")


def _brief_get(brief, key, default=None):
    """Read a brief field TOLERANTLY — works for a dict brief AND a NeutralContract dataclass.

    THE BRIEF-SHAPE BUG (JAIL-WIRING): ``brief.assemble_neutral`` returns a ``NeutralContract``
    DATACLASS (no ``.get``), but the production chokepoint hands that dataclass straight to the
    adapter. The old ``(neutral_brief or {}).get(...)`` reads raised ``AttributeError`` on the
    dataclass (the existing adapter tests passed only because they handed a DICT brief). This helper
    does ``dict.get`` for a mapping and ``getattr`` for the dataclass, so BOTH shapes read the same
    fields (``role_variant`` / ``containment_profile``). A None brief yields the default.
    """
    if brief is None:
        return default
    if isinstance(brief, dict):
        return brief.get(key, default)
    return getattr(brief, key, default)


def _resolve_containment(neutral_brief, level_config) -> dict | None:
    """Resolve the §2.5a ``containment_profile`` block, or None for the unjailed (dry-run) path.

    SECURITY.md §7 wires the write-jail at the DAEMON §6.2 pane-launch command. The chokepoint
    resolves the containment block (machine baseline -> per-spawn override -> resolved block,
    §2.5a/§2.5b) and rides it on the brief / level_config. When NO block is present (the pure
    dry-run argv/env assembly that the Increment-9 adapter tests exercise), the pane is the bare
    ``env -i`` isolator — UNJAILED — so the dry-run boundary stays a deterministic, sandbox-free
    assembly. When a block IS present, ``pin_and_open`` renders the §2.3 profile and wraps the
    pane with ``sandbox-exec`` (§7.1).

    The block is read from ``neutral_brief['containment_profile']`` first (the per-spawn override
    the chokepoint flattens onto the brief), then ``level_config.containment_profile``. It must
    already be RESOLVED to the §2.5a shape — WORKROOT/TMPDIR/CONFIG/HOME (+ optional
    READ_DENY_ROOT / extra_read_denies / extra_write_roots). ``sandbox.render_profile`` owns the
    §2.4 realpath-canonicalization; the chokepoint hands it logical paths.
    """
    block = _brief_get(neutral_brief, "containment_profile")
    if block is None:
        block = getattr(level_config, "containment_profile", None)
    if not block:
        return None
    return dict(block)


class ClaudeCodeAdapter(RuntimeAdapter):
    """The concrete Claude-Code adapter (the H40 boot recipe; OAuth-only by construction)."""

    def __init__(self, tmux=None):
        # The tmux transport seam. Production wires ``harnessd.spawn.tmux``; the dry-run wires a
        # mock that records create_detached without a real exec. Attribute-injectable too.
        if tmux is None:
            from harnessd.spawn import tmux as _tmux

            tmux = _tmux
        self.tmux = tmux

    @staticmethod
    def interactive_prompt_signature(pane_text: str) -> str | None:
        """Recognize only measured Claude Code chooser shapes for safe Escape cancellation."""
        text = str(pane_text or "")
        lowered = text.lower()
        if all(
            marker in lowered
            for marker in ("stop and wait", "add funds", "upgrade")
        ):
            return "claude-rate-limit-chooser"
        selected_numbered_option = re.search(r"(?m)^\s*❯\s*\d+\.", text) is not None
        if not selected_numbered_option:
            return None
        if "enter to select" in lowered and "esc to cancel" in lowered:
            return "claude-choice-select-cancel"
        if "enter to confirm" in lowered and "esc to cancel" in lowered:
            return "claude-choice-confirm-cancel"
        if "esc to cancel" in lowered:
            return "claude-choice-cancel"
        return None

    # ---- the §2.11 pieces, each a single seam ----------------------------------------------

    def verify_binary(self, level_config=None) -> None:
        """Confirm the configured model+runtime is pinned BEFORE the child runs (E32).

        Pure, NO-exec in v1: confirms ``config.PINNED_BINARY_VERSION`` is set (the pin is the
        npm version + isolated prefix, PINNED-CC). ``config.PINNED_BINARY_HASH`` is a v1
        placeholder (None) so the sha256 probe is DEFERRED (FORK-VERIFY) — the dry-run forbids a
        real ``claude --version`` exec. Raises if the pinned version seat is missing.
        """
        if not config.PINNED_BINARY_VERSION:
            raise oauth_guard.SpawnFailure(
                "pinned Claude binary version is not configured (config.PINNED_BINARY_VERSION) — "
                "the pinned-binary seat must exist before a spawn (E32)"
            )
        # config.PINNED_BINARY_HASH is None in v1 (gitignored install): the sha256 verification is
        # the documented commissioning seam, not skippable silence — see FORK-VERIFY in the docstring.

    def _deterministic_trust(self, env: dict, containment=None, cwd=None) -> None:
        """Deterministic first-boot trust — pre-seed CLAUDE_CONFIG_DIR so NO startup dialog /
        permission prompt appears, NOT a send-keys race (SECURITY.md §deterministic-trust).

        A real interactive spawn hits BLOCKING dialogs (workspace trust, the bypass-permissions
        warning, per-tool prompts) that would FREEZE an unattended agent. ``cc_config.seed_trust``
        writes the acceptance keys for the agent's WORKSPACE into ``.claude.json``/``settings.json``
        BEFORE launch, so the agent boots straight to working. Verified live: a fresh workspace
        pre-seeded this way shows zero dialogs.

        Seeded for the ACTUAL pane cwd on EVERY spawn (the transport increment) — jailed (the
        WORKROOT) AND unjailed (the node-workspace cwd the pane now boots in, F18 ``-c``): an
        untrusted cwd freezes an UNJAILED agent on the trust dialog exactly the same way.
        ``seed_trust`` itself skips cleanly when CLAUDE_CONFIG_DIR is not a real on-disk dir
        (the dry-run placeholder), so the pure-assembly tests stay side-effect-free.
        """
        config_dir = env.get("CLAUDE_CONFIG_DIR")
        workroot = (containment or {}).get("WORKROOT") or cwd
        if config_dir and workroot:
            cc_config.seed_trust(config_dir, workroot)
        return None

    def _write_profile(self, env, containment, session_name, profile_text) -> str:
        """Write the rendered ``.sb`` to a stable per-session path the seatbelt reads, return it.

        The profile file lives under the jail's writable CONFIG dir (CLAUDE_CONFIG_DIR — a §2.3
        write-allow root, realpath-canonicalized) so sandbox-exec can read it AND the spawning
        daemon may rewrite it on resume/necro (the same jail is re-applied — §7). Falls back to
        the containment CONFIG/TMPDIR when CLAUDE_CONFIG_DIR is not a real on-disk path (e.g. the
        ``$HARNESS/...`` placeholder the dry-run carries). The filename is keyed to the collapsed
        session so concurrent spawns never collide.
        """
        config_dir = env.get("CLAUDE_CONFIG_DIR", "")
        base = config_dir if os.path.isdir(config_dir) else (
            containment.get("CONFIG") or containment.get("TMPDIR") or containment.get("WORKROOT")
        )
        return sandbox.write_profile(
            profile_text,
            base_dir=str(base),
            session_name=session_name,
        )

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env) -> SpawnResult:
        """Pin, OAuth-gate, open the from-empty pane, record the facts (the H40 recipe)."""
        env = dict(env)  # never mutate the caller's dict

        # role_variant rides the brief / level_config (role-as-documents), NEVER the argv. Read the
        # brief field TOLERANTLY (dict brief OR NeutralContract dataclass) — the brief-shape bug fix.
        role_variant = (
            _brief_get(neutral_brief, "role_variant")
            or getattr(level_config, "role_variant", None)
            or getattr(level_config, "level", None)
        )

        # (1) Pin the binary (model+runtime) before anything child-facing runs (E32).
        self.verify_binary(level_config)

        # (1a) Resolve the §2.5a containment block FIRST — it decides both the jail AND the
        #      permission posture: --dangerously-skip-permissions is added ONLY for a jailed spawn
        #      (the safety invariant — auto-approve is safe ONLY because the seatbelt jail is the
        #      structural bound, SECURITY.md constraint 4; an UNJAILED dry-run never auto-approves)
        #      …with ONE explicit exception: the USER-APPROVED supervised-smoke override
        #      (level_config.unjailed_skip_permissions — see the loud branch at (2)).
        containment = _resolve_containment(neutral_brief, level_config)

        # (2) Assemble argv: the per-spawn COMPOSED identity bundle. The shared base prompt still
        #     leads the file, but the selected level identity trio is auto-loaded there rather than
        #     depending on a first-turn read. No --bare/--append-system-prompt/--agents/--agent. When jailed, add
        #     --dangerously-skip-permissions so the unattended agent auto-approves its own tool calls
        #     (the jail bounds the blast radius; every permission prompt is superfluous and would only
        #     FREEZE the agent with no human at the pane — SECURITY.md §362).
        #
        #     The --system-prompt-file value is ABSOLUTE (resolved against HARNESS_ROOT — the
        #     config.py NOTE's resolution contract): the pane now boots in the NODE's workspace
        #     (cwd below), so a repo-relative path would dangle. The recorded
        #     SpawnResult.system_prompt_file records the composed file actually loaded.
        #
        #     --model is derived from level_config.model via config.CC_MODEL_FLAGS (probed live:
        #     the pinned CC chooses its own default without it — the recorded model was a lie).
        #     An unmapped model adds NO flag (explicit mapping, never a guessed id); model_used
        #     below remains the recorded INTENT (the E32 fact-checker is deferred F17 territory).
        #     --effort is the exact owner-calibrated LevelConfig value; pinned CC 2.1.152 accepts
        #     both shipped tiers (high/xhigh) natively, so no adapter mapping or fallback exists.
        cc = str(_harness_root() / _PINNED_CC)
        # Identity AUTO-LOAD (LR-4 cure): the per-spawn COMPOSED bundle (shared prompt + the
        # level trio), never the bare shared constant — see _compose_identity_prompt.
        identity_path, identity_hash = _compose_identity_prompt(
            level_config,
            _brief_get(neutral_brief, "workspace"),
            _brief_get(neutral_brief, "launch_packet_file"),
        )
        argv = [cc, "--system-prompt-file", str(identity_path)]
        cc_model = config.CC_MODEL_FLAGS.get(getattr(level_config, "model", None))
        if cc_model:
            argv += ["--model", cc_model]
        argv += ["--effort", level_config.reasoning_effort]
        if _fence_native_agent_tool(level_config):
            argv += ["--disallowed-tools", _NATIVE_AGENT_TOOL]
        # --session-id pins CC's session uuid to OURS, so the recorded transcript_path is the
        # file CC actually writes (verify-new-turn's stat target). Without the pin CC mints its
        # own uuid and the detector watches a file that never exists (2026-06-11 live-run
        # finding: the idle ladder failed a healthy L1 as watchdog_nonresponse). Minted fresh
        # per spawn attempt — never reused across incarnations (CC refuses a duplicate id).
        session_uuid = str(uuid.uuid4())
        argv += ["--session-id", session_uuid]
        if containment is not None:
            containment = sandbox.finalize_runtime_state(
                containment,
                runtime="claude-code",
                state_root=str(containment["CONFIG"]),
                session_uuid=session_uuid,
            )
        if containment is not None:
            argv.append("--dangerously-skip-permissions")
            permission_posture = "jailed-skip-permissions"
        elif getattr(level_config, "unjailed_skip_permissions", False):
            # SUPERVISED-SMOKE OVERRIDE (USER-APPROVED, 2026-06-10) — the LOUD, EXPLICIT
            # decoupling of SECURITY.md constraint 4 ("skip-permissions INSIDE the jail …
            # containment bounds the blast radius"). The user's decision for the first
            # supervised live run: "Unjailed + dangerously skip permissions. It is a small
            # run, the risk of something catastrophic happening is minimal." The knob is
            # NEVER read from the environment here — it rides ONLY level_config (stamped by
            # commissioning.build_runtime / config.get_level_config from the strict
            # HARNESS_UNJAILED_SKIP_PERMISSIONS=1 opt-in) and NEVER the brief (§2.5b: a
            # per-spawn brief may TIGHTEN, never RELAX — an injected brief cannot
            # self-escalate to auto-approve). Journaled below as
            # permission_posture="unjailed-skip-permissions-override" (SECURITY.md §4.3).
            # RETIREMENT: the jail tier (REMEDIATION F9–F13) retires this branch.
            argv.append("--dangerously-skip-permissions")
            permission_posture = "unjailed-skip-permissions-override"
        else:
            permission_posture = "unjailed-prompting"

        # Install the truthful five-edge hook set through an explicit per-seat settings file.
        # Never mutate the shared pinned CLAUDE_CONFIG_DIR. Bare adapter dry-runs carry no turn
        # surface, so their argv and side-effect boundary remain unchanged.
        workspace_for_hooks = _brief_get(neutral_brief, "workspace")
        if (
            workspace_for_hooks
            and _brief_get(neutral_brief, "turn_hook_profile") == turn_state.CLAUDE_FULL_EDGES
            and _brief_get(neutral_brief, "turn_runtime_root")
        ):
            _path, seat = addressing.split_address(tmux_target)
            hook_owner_token = turn_state.hook_owner_token(
                runtime_root=_brief_get(neutral_brief, "turn_runtime_root"),
                node_address=tmux_target,
            )
            hook_command = turn_state.hook_argv(
                python_executable=sys.executable,
                runtime_root=_brief_get(neutral_brief, "turn_runtime_root"),
                node_address=tmux_target,
                owner_token=hook_owner_token,
                runtime="claude-code",
            )
            hook_settings = cc_config.write_turn_hook_settings(
                str(workspace_for_hooks),
                seat=seat,
                hook_argv=hook_command,
            )
            argv += ["--settings", str(hook_settings)]

        # (3) The from-empty pane (the SAME isolator seam the wrapper uses). The adapter builds
        #     the EXACT pane it hands to create_detached so the guard checks the real pane vector;
        #     create_detached is wrapping-idempotent (it will not re-wrap an `env -i`-led argv).
        pane_argv = self.tmux.build_pane_argv(env, argv)

        # (4) OAuth-only gate BEFORE create_detached: the runtime-AGNOSTIC negative invariant +
        #     the pane-env-isolation guard, checking the env the PANE WILL ACTUALLY SEE
        #     (tmux.server_env()). A forbidden env raises ApiKeyForbidden here -> NO actor opens.
        oauth_guard.assert_oauth_only(env, argv, pane_argv, self.tmux.server_env())

        # (5) The CLAUDE-SPECIFIC positive token check (DISTINCT AuthExpired; refresh-the-token,
        #     not a model outage). Lives in this path, NOT the shared gate.
        oauth_guard.check_credential_health(env)

        # (6) The §2.5a containment block was resolved at (1a). When present, the pane is JAILED
        #     (§2.3 profile + sandbox-exec wrap, §7.1) and its env legitimately carries the §2.3
        #     containment vars (CLAUDE_CODE_TMPDIR/HOME + the cache-redirection set) ON TOP of the 4
        #     isolation vars. When absent (the dry-run boundary), the pane is the bare `env -i`
        #     isolator and the env MUST be EXACTLY the 4 isolation vars.

        # (6a) ENFORCE the OAuth-only isolation floor. UNJAILED: the env must be the 4 isolation
        #      vars + (LR-2, user posture decision 2026-06-11: allowlist->denylist for the PoC
        #      phase) the named NON-CREDENTIAL extras below — PATH (agent subshells failed to
        #      find python3/head every turn; zero security value in omitting it) and TERM. Any
        #      OTHER extra still refuses (the credential-leak surface stays closed).
        #      JAILED: the 4 isolation vars are the REQUIRED floor; the extra vars are the named
        #      §2.3 containment set (no raw API key — already rejected by the OAuth gate above).
        #      Runs AFTER the OAuth gate + credential check (so api-key -> ApiKeyForbidden and a
        #      missing token -> AuthExpired keep their SPECIFIC classes) but BEFORE create_detached.
        if containment is None:
            allowed_extras = {"PATH", "TERM"}  # LR-2: non-credential ergonomics, never a secret
            extra = set(env) - _ISOLATION_ENV_KEYS - allowed_extras
            missing = _ISOLATION_ENV_KEYS - set(env)
            if extra or missing:
                raise oauth_guard.SpawnFailure(
                    "pane env must be the 4 isolation vars (+ optionally PATH/TERM — LR-2) "
                    "(DAEMON §6.2 amended); refusing a widened/incomplete env. "
                    f"extra={sorted(extra)} missing={sorted(missing)}"
                )
        else:
            missing = _ISOLATION_ENV_KEYS - set(env)
            if missing:
                raise oauth_guard.SpawnFailure(
                    "jailed pane env must carry the 4 isolation vars (DAEMON §6.2) as its floor; "
                    f"missing={sorted(missing)}"
                )

        # (6b) Resolve the PANE CWD: the node's workspace (the brief's ``workspace`` pointer — the
        #      nested addressing.node_dir the chokepoint registered). The agent boots WHERE its
        #      brief lands, so the kickoff pointer's relative reads (brief.md, .inbox.<seat>.jsonl)
        #      agree with the pane cwd. Ensured on disk (a `-c` into a missing dir fails the
        #      new-session). Absent workspace (the bare adapter-level dry-run) -> no cwd, the pane
        #      inherits the server default exactly as before.
        cwd = _brief_get(neutral_brief, "workspace")
        if cwd:
            cwd = str(cwd)
            try:
                Path(cwd).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass  # an un-creatable workspace surfaces at create_detached, loudly
            # REALPATH-canonicalize (probed live): CC keys its trust map by the REALPATH the
            # session opens (e.g. macOS /var/... -> /private/var/...). A symlinked cwd seeded
            # under the logical path MISSES the trust lookup and the agent freezes on the
            # trust dialog — so the cwd handed to seed_trust AND to tmux -c is the realpath.
            cwd = os.path.realpath(cwd)

        # (7) Deterministic first-boot trust (pre-seeded config dir; no send-keys race) — KILLS every
        #     startup dialog/permission prompt for the agent's workspace (trust dialog + bypass warning
        #     + per-tool prompts) so an unattended agent boots straight to working, never frozen on a
        #     dialog (SECURITY.md §deterministic-trust; the jail is the bound, prompts are superfluous).
        #     Seeded for the ACTUAL pane cwd on EVERY spawn — unjailed included (the trust dialog
        #     freezes an unjailed agent just the same).
        self._deterministic_trust(env, containment, cwd)

        # (8) Render the §2.3 seatbelt profile and wrap the env-i pane with sandbox-exec (§7.1) when
        #     a containment block is resolved. The wrapped vector — `sandbox-exec -f <profile>.sb
        #     env -i <K=V…> <binary> <flags>` — IS the detached pane's launch command. The §2.4
        #     canonicalization + the cache-redirection env are owned by the sandbox seam. create_-
        #     detached is wrapping-idempotent for the bare-`env -i` head; the sandbox-wrapped vector
        #     (head = sandbox-exec) is passed through verbatim.
        session_name = addressing.session_name_for(tmux_target)
        launch_argv = pane_argv
        containment_posture = None
        if containment is not None:
            config_dir = env.get("CLAUDE_CONFIG_DIR", "")
            base = config_dir if os.path.isdir(config_dir) else (
                containment.get("CONFIG")
                or containment.get("TMPDIR")
                or containment.get("WORKROOT")
            )
            try:
                prepared = sandbox.prepare_launch(
                    pane_argv,
                    containment,
                    base_dir=str(base),
                    session_name=session_name,
                )
            except sandbox.ProfileApplicationError as exc:
                raise SpawnFailure(
                    str(exc),
                    failure_class="containment_apply_failed",
                ) from exc
            launch_argv = prepared.argv
            containment_posture = prepared.posture
            if not prepared.applied:
                permission_posture = "degraded-unjailed-skip-permissions"

        # (9) Open the detached actor with the pane vector (wrapped when jailed, bare env -i when not),
        #     booting in the node workspace when one is contracted (-c cwd). create_detached returns
        #     the CANONICAL live target '<session>:<window>.<pane>' (tmux's own post-rename report,
        #     F18/OSA-01) — THAT is the recorded tmux_target, never the requested name (which tmux
        #     may rewrite) and never a guessed ':0.0' suffix (base-index may differ).
        if cwd:
            canonical_target = self.tmux.create_detached(session_name, launch_argv, env, cwd=cwd)
        else:
            canonical_target = self.tmux.create_detached(session_name, launch_argv, env)

        # (8) Record the facts (config = intent, model_used = fact). session_uuid was minted at
        #     argv assembly (--session-id pins it into CC); the transcript path is the encoded
        #     REALPATH-cwd file CC will write under that uuid.
        transcript_path = _transcript_path(env, cwd, session_uuid)

        return SpawnResult(
            ok=True,
            session_uuid=session_uuid,
            model_used=_model_used(level_config),
            role_variant=role_variant,
            system_prompt_file=str(identity_path),
            system_prompt_file_hash=identity_hash,
            tmux_target=canonical_target,
            transcript_path=transcript_path,
            failure_class=None,
            argv=tuple(argv),
            env=dict(env),
            permission_posture=permission_posture,
            containment_posture=containment_posture,
            launch_packet_file=_brief_get(neutral_brief, "launch_packet_file"),
            launch_packet_hash=_brief_get(neutral_brief, "launch_packet_hash"),
            launch_surface_source_hash=_brief_get(neutral_brief, "launch_surface_source_hash"),
            reference_map_file=_brief_get(neutral_brief, "reference_map_file"),
            reference_map_hash=_brief_get(neutral_brief, "reference_map_hash"),
            reference_map_json_file=_brief_get(neutral_brief, "reference_map_json_file"),
            launch_surface_version=_brief_get(neutral_brief, "launch_surface_version"),
        )
