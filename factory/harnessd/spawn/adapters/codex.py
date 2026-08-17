"""codex — the REAL Codex-runtime adapter (E4 / DEFERRED-REGISTER O1, retires the fail-loud
stub): GPT-5.5 on the Codex harness at L5, per runtime-and-model-map E32.

Probed LIVE against codex-cli 0.128.0 + the ChatGPT account (2026-06-11, preference 2 — real
over dummy; every mechanism below is a probe result, not an assumption):

  * CODEX_HOME redirects ~/.codex fully. The pinned canonical home is
    ``.codex-pinned/config``; its auth file symlinks to the machine's single
    ``~/.codex/auth.json`` token lineage. Worker seats run from isolated child
    homes under ``.codex-pinned/seats/`` seeded with fresh access/id credentials
    and a present but non-usable refresh token field.
  * NO SYSTEM-PROMPT INJECTION (user decision 2026-06-11): "it's sufficient to not change the
    existing system message that it uses — it basically just needs precise technical
    instructions… what it's not told to do, it probably doesn't do." Codex's native base
    instructions stay; ALL harness instruction rides the brief + kickoff (maximally explicit,
    decision-complete — the codex-audit discipline, runtime-and-model-map §135). The recorded
    ``system_prompt_file`` is the explicit sentinel below, never the CC shared prompt.
  * ``-m gpt-5.5`` is accepted AND the rollout header records ``"model":"gpt-5.5"`` as FACT —
    the silent-fallback divergence runtime-and-model-map warns about is detectable downstream.
  * TRANSCRIPT surface: ``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``; the
    header line carries the session ``id`` + the pane ``cwd``; the file GROWS per turn (the
    detector's verify-new-turn stat works unchanged). There is NO session-id flag, so the
    adapter DISCOVERS the rollout post-boot: a bounded poll for a rollout whose header cwd ==
    the node workspace realpath, created at/after boot. Undiscoverable in the deadline =
    SpawnFailure('transcript_undiscovered') — fail-loud, never a blind binding (the 2026-06-11
    CC watchdog-blindness lesson, applied here from day one).
  * First-boot TRUST dialog persists as ``[projects."<REALPATH>"] trust_level = "trusted"`` in
    the worker CODEX_HOME/config.toml — seeded deterministically pre-spawn (the CC realpath
    precedent).
  * The TUI idle marker is '›' (CC's '❯') — surfaced as PROMPT_MARKER for the per-runtime
    marker map (kickoff gate / prod_precondition / wake delivery).
  * Posture: the runtime's existing inner flags remain explicit. Production blinders wrap the
    unchanged Codex pane in the SAME external seatbelt profile used for Claude, finalized only
    after the exact worker CODEX_HOME is minted. The canonical refresh-token home is never granted.

OAuth-only: the shared negative gate (oauth_guard.assert_oauth_only) runs unchanged — no
ANTHROPIC_API_KEY / OPENAI_API_KEY ever rides the pane; Codex auth is the auth.json file
inside the Codex home (file-based, never an env credential).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from harnessd import addressing, config, store, turn_state
from harnessd.runtime_failures import (
    AUTH_EXPIRED,
    AUTH_RATE_LIMITED,
    codex_error_payload,
    runtime_failure_class_from_codex_error,
)
from harnessd.spawn import codex_auth, oauth_guard, sandbox

from .base import RuntimeAdapter, SpawnResult
from harnessd.spawn.oauth_guard import SpawnFailure  # the shared typed spawn-refusal (E32)

# The recorded system_prompt_file sentinel: Codex runs its NATIVE base instructions (user
# decision) — this string documents that fact on the binding instead of pretending a file.
CODEX_NATIVE_INSTRUCTIONS = "(codex-native-base-instructions)"

# The TUI idle-input marker (probed 0.128.0) — the per-runtime prompt-marker map reads this.
PROMPT_MARKER: str = "›"

# The BOOT PROMPT — the optional [PROMPT] argv the codex TUI accepts. Two jobs: (1) the TUI
# creates its rollout LAZILY on the FIRST TURN (probed: boot 20:01, rollout appeared at the
# 20:04 first message), so starting a turn at boot is what makes the rollout discoverable
# within the spawn path's deadline; (2) it is the codex-idiomatic first instruction (user
# decision: precise technical instructions; the brief carries everything else).
BOOT_PROMPT: str = (
    "Read the file brief.md in your current working directory and follow its instructions "
    "exactly. Further messages arrive as appended lines in the .inbox.*.jsonl file in this "
    "directory; when a message tells you to check it, read any lines you have not yet read."
)

CODEX_BOOT_PROMPT_FILE = ".codex-boot-prompt.md"
CODEX_CA_BUNDLE_FILE = "harness-ca-bundle.pem"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/cert.pem")
SYSTEM_KEYCHAIN = Path("/Library/Keychains/System.keychain")
ADGUARD_CA_COMMON_NAME = "Adguard Personal CA"

_BOOT_PROMPT_LOADER = (
    'prompt_file="$1"\n'
    "shift\n"
    'prompt="$(cat "$prompt_file")" || exit 1\n'
    'exec "$@" "$prompt"'
)


def _brief_value(brief, key, default=None):
    if brief is None:
        return default
    if isinstance(brief, dict):
        return brief.get(key, default)
    return getattr(brief, key, default)


def _boot_prompt(level_config, neutral_brief=None) -> str:
    """The per-spawn boot prompt: identity AUTO-LOAD + task-list-first + the BOOT_PROMPT base.

    LR-4 cure (user ruling 2026-06-12): codex gets NO system-prompt injection (its native
    instructions stay — user decision), so its identity auto-load rides the boot prompt as an
    EXPLICIT ordered reading list (codex follows literal instructions; what it is told to read,
    it reads). Then the task-list-first / durable-plan discipline (Run-2: 11 of 15
    return-contract bounces were reporting-discipline — the boot prompt is the guaranteed moment
    to set it), then the
    BOOT_PROMPT base (brief + inbox)."""
    launch_packet_file = _brief_value(neutral_brief, "launch_packet_file")
    if launch_packet_file:
        try:
            launch_body = Path(str(launch_packet_file)).read_text(encoding="utf-8")
        except OSError as exc:
            raise SpawnFailure(
                f"launch packet {launch_packet_file} is unreadable: {exc}",
                failure_class="identity_missing",
            ) from exc
        return (
            "Your launch packet is already loaded below. It is the startup surface for normal "
            "work; use .reference-map.md only for concrete lookups the task requires.\n\n"
            "--- LAUNCH PACKET ---\n"
            f"{launch_body}\n"
            "--- END LAUNCH PACKET ---\n\n"
            f"Then: {BOOT_PROMPT} First create or refresh the native plan tool from the launch "
            "packet's Startup Task List Seed. Keep it high-level and at your role altitude. After "
            "bounded orientation on the task package, write or update plan.md in this directory as "
            "the durable mirror — a one-line goal plus a task checklist whose final three items are: "
            "fill report.md, verify your requirement-ID citations, sign off. Keep the plan tool and "
            "plan.md current as you work."
        )

    from harnessd.spawn import brief as _brief

    level = (getattr(level_config, "level", None) or "").strip() or "L5"
    root = _harness_root()
    docs = ", ".join(str(root / rel) for rel in _brief.identity_docs(level))
    return (
        f"First, read these identity documents in order — they define who you are in this "
        f"system: {docs}. Then: {BOOT_PROMPT} First create or refresh the native plan tool with a "
        f"high-level role-appropriate checklist. After bounded orientation on the task package, "
        f"write or update plan.md in this directory as the durable mirror — a one-line goal plus a "
        f"task checklist whose final three items are: fill report.md, verify your requirement-ID "
        f"citations, sign off. Keep the plan tool and plan.md current as you work."
    )

# Rollout discovery bounds. MEASURED (smoke, 0.128.0): the TUI opens its rollout when the
# boot-prompt turn actually starts streaming — ~100s after create_detached on a cold boot
# (NOT the few seconds `codex exec` takes). The deadline covers that with margin; the cost is
# a blocking wait on the spawn path per codex actor (FOLLOW-UP: async discovery via a later
# sweep so one slow codex boot does not stall the daemon's whole tick).
DISCOVERY_DEADLINE_S: float = 150.0
DISCOVERY_POLL_S: float = 1.0
AUTH_ERROR_SCAN_DEADLINE_S: float = 1.0
AUTH_ERROR_SCAN_POLL_S: float = 0.1


def _harness_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _pinned_codex() -> Path:
    return _harness_root() / config.PINNED_CODEX_BINARY


def _pinned_home() -> Path:
    return _harness_root() / config.PINNED_CODEX_HOME


def _prepare_adguard_ca_bundle(worker_home: Path) -> Path | None:
    """Copy the exact system-trusted AdGuard CA into this Codex worker's root bundle."""
    security = str(os.environ.get("HARNESSD_SECURITY") or "").strip()
    if not security or not SYSTEM_CA_BUNDLE.is_file() or not SYSTEM_KEYCHAIN.is_file():
        return None
    try:
        standard_roots = SYSTEM_CA_BUNDLE.read_text(encoding="utf-8")
        exported = subprocess.run(
            [
                security,
                "find-certificate",
                "-c",
                ADGUARD_CA_COMMON_NAME,
                "-p",
                str(SYSTEM_KEYCHAIN),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    pem_marker = "-----BEGIN CERTIFICATE-----"
    adguard_ca = str(exported.stdout or "")
    if (
        exported.returncode != 0
        or pem_marker not in standard_roots
        or pem_marker not in adguard_ca
    ):
        return None
    bundle = worker_home / CODEX_CA_BUNDLE_FILE
    content = standard_roots.rstrip() + "\n" + adguard_ca.rstrip() + "\n"
    try:
        store.atomic_replace(bundle, lambda handle: handle.write(content))
        os.chmod(bundle, 0o600)
    except OSError:
        try:
            bundle.unlink()
        except OSError:
            pass
        return None
    return bundle


class CodexAdapter(RuntimeAdapter):
    """RuntimeAdapter for the Codex harness (GPT-5.5 at L5)."""

    def __init__(self, tmux=None):
        if tmux is None:
            from harnessd.spawn import tmux as tmux_mod
            tmux = tmux_mod
        self.tmux = tmux

    @staticmethod
    def interactive_prompt_signature(pane_text: str) -> str | None:
        """Fail closed until the pinned Codex TUI supplies a captured chooser fixture.

        Codex seats run ``approval_policy = never``.  Inventing a generic matcher from Claude
        text would make a runtime-specific safety decision from unmeasured vocabulary.
        """
        return None

    # ------------------------------------------------------------------ #
    # E32 step 1 — pin the binary (pure, no-exec confirmation in v1).
    # ------------------------------------------------------------------ #
    def verify_binary(self, level_config) -> None:
        binary = _pinned_codex()
        if not binary.is_file():
            raise SpawnFailure(
                f"pinned codex binary missing at {binary} (npm @openai/codex@"
                f"{config.PINNED_CODEX_VERSION} into .codex-pinned/)",
                failure_class="runtime_down",
            )

    # ------------------------------------------------------------------ #
    # Rollout discovery — the transcript surface (probe result; injectable for tests).
    # ------------------------------------------------------------------ #
    @staticmethod
    def discover_rollout(
        sessions_root: Path,
        cwd_realpath: str,
        since_epoch: float,
        *,
        deadline_s: float = DISCOVERY_DEADLINE_S,
        poll_s: float = DISCOVERY_POLL_S,
    ) -> tuple:
        """Find the rollout the freshly-booted pane is writing: the newest ``rollout-*.jsonl``
        under ``sessions_root`` whose mtime >= since_epoch AND whose header line records
        ``cwd == cwd_realpath``. Returns (session_uuid, transcript_path); raises
        SpawnFailure('transcript_undiscovered') past the deadline — fail-loud, a binding with a
        blind transcript path is exactly the 2026-06-11 watchdog-blindness bug class."""
        deadline = time.monotonic() + deadline_s
        while True:
            candidates = []
            if sessions_root.is_dir():
                for p in sessions_root.rglob("rollout-*.jsonl"):
                    try:
                        if p.stat().st_mtime + 1.0 >= since_epoch:
                            candidates.append(p)
                    except OSError:
                        continue
            for p in sorted(candidates, key=lambda q: q.stat().st_mtime, reverse=True):
                try:
                    header = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
                except (OSError, ValueError, IndexError):
                    continue
                # The header line nests the session meta under "payload" (the TUI rollout shape:
                # {"timestamp":…, "type":"session_meta", "payload": {"id":…, "cwd":…}}); tolerate
                # the flat shape too (the exec probe's unwrapped reading).
                meta = header.get("payload") if isinstance(header.get("payload"), dict) else header
                if meta.get("cwd") == cwd_realpath and meta.get("id"):
                    return str(meta["id"]), str(p)
            if time.monotonic() >= deadline:
                raise SpawnFailure(
                    f"no codex rollout discovered under {sessions_root} for cwd "
                    f"{cwd_realpath!r} within {deadline_s}s — refusing a blind binding",
                    failure_class="transcript_undiscovered",
                )
            time.sleep(poll_s)

    @staticmethod
    def _rollout_auth_error(row: dict) -> str | None:
        payload = codex_error_payload(row)
        if payload is None:
            return None
        message, code, _timestamp = payload
        if runtime_failure_class_from_codex_error(message, code) == AUTH_EXPIRED:
            return message or "codex authorization failed"
        return None

    @staticmethod
    def _rollout_rate_limit_error(row: dict) -> str | None:
        payload = codex_error_payload(row)
        if payload is None:
            return None
        message, code, _timestamp = payload
        if runtime_failure_class_from_codex_error(message, code) == AUTH_RATE_LIMITED:
            return message or "codex rate limit reached"
        return None

    @staticmethod
    def _rollout_has_first_turn_progress(row: dict) -> bool:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("type") == "response_item" and payload.get("type") == "message":
            return payload.get("role") == "assistant"
        if row.get("type") == "event_msg":
            if payload.get("type") in {"agent_message", "assistant_message"}:
                return True
            if payload.get("type") == "task_complete" and payload.get("last_agent_message"):
                return True
        return False

    @classmethod
    def fail_on_rollout_auth_error(
        cls,
        transcript_path: str,
        *,
        deadline_s: float = AUTH_ERROR_SCAN_DEADLINE_S,
        poll_s: float = AUTH_ERROR_SCAN_POLL_S,
    ) -> None:
        """Detect immediate Codex auth/rate-limit failures after rollout discovery.

        Rollout discovery proves only that the TUI created a transcript. The
        first actual turn can still fail immediately with a persisted
        ``event_msg`` error (observed live on 2026-06-16: refresh token already
        used). Scan briefly for that row and same-window rate-limit rows, then
        convert them into typed spawn-failure classes instead of letting the
        watchdog rediscover them later as generic nonresponse. This is not a
        health proof; once the first turn has progressed past the immediate
        runtime-error window, the normal watchdog remains responsible for
        liveness.
        """
        path = Path(transcript_path)
        deadline = time.monotonic() + deadline_s
        seen_rows = 0
        while True:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines[seen_rows:]:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                message = cls._rollout_auth_error(row)
                if message:
                    raise SpawnFailure(message, failure_class=AUTH_EXPIRED)
                message = cls._rollout_rate_limit_error(row)
                if message:
                    raise SpawnFailure(message, failure_class=AUTH_RATE_LIMITED)
                if cls._rollout_has_first_turn_progress(row):
                    return
            seen_rows = len(lines)
            if time.monotonic() >= deadline:
                return
            time.sleep(poll_s)

    # ------------------------------------------------------------------ #
    # The spawn (mirrors the CC adapter's pin -> gate -> open -> record recipe).
    # ------------------------------------------------------------------ #
    def pin_and_open(self, neutral_brief, level_config, tmux_target, env) -> SpawnResult:
        env = dict(env)

        def _brief_get(key, default=None):
            return _brief_value(neutral_brief, key, default)

        role_variant = (
            _brief_get("role_variant")
            or getattr(level_config, "role_variant", None)
            or getattr(level_config, "level", None)
        )

        # (1) Pin the binary before anything child-facing runs (E32).
        self.verify_binary(level_config)

        # One physical local-filesystem boundary spans both runtimes.  The chokepoint derives the
        # neutral policy; this adapter binds it to the exact worker CODEX_HOME after minting that
        # home below, then wraps the unchanged Codex inner pane externally.
        containment = _brief_get("containment_profile")

        cwd = _brief_get("workspace")
        if cwd:
            cwd = str(cwd)
            try:
                Path(cwd).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            cwd = os.path.realpath(cwd)

        # (2) argv: the pinned codex + the EXPLICIT model flag (never a guessed id) + posture.
        codex = str(_pinned_codex())
        codex_argv = [codex]
        model_flag = config.CODEX_MODEL_FLAGS.get(getattr(level_config, "model", None))
        if model_flag:
            codex_argv += ["-m", model_flag]
        # NESTED-SANDBOX LAW (SECURITY.md §8.1(b), the mirror of Claude's jailed-skip-permissions):
        # a process already under a RESTRICTIVE seatbelt cannot apply a second one — macOS answers
        # `sandbox_apply: Operation not permitted`, and that refusal is itself a ratified
        # escape-block (`test_nested_sandbox_exec_blocked`). Codex sandboxes every command and patch
        # by re-invoking sandbox-exec, so under our jail it cannot run ANYTHING; with
        # `approval_policy = "never"` it cannot even ask, and a seat that cannot write cannot write
        # its own FAILED sign-off (run-5 plan-alignment cell: both Codex seats burned usage looping
        # on an unsatisfiable turn-end checklist). The bypass flag exists for exactly this — "solely
        # for running in environments that are externally sandboxed" — so an externally jailed seat
        # takes it and the harness seatbelt remains the one and only containment. This is NOT a
        # blinders-mode question: the write jail that blocks nesting is rendered in observe too.
        externally_jailed = containment is not None
        if externally_jailed or getattr(level_config, "unjailed_skip_permissions", False):
            # The user-approved PoC posture rendered in codex terms (probed: "YOLO mode").
            codex_argv.append("--dangerously-bypass-approvals-and-sandbox")
            permission_posture = (
                "externally-jailed-bypass" if externally_jailed
                else "unjailed-skip-permissions-override"
            )
        else:
            permission_posture = "unjailed-prompting"
        boot_prompt = _boot_prompt(level_config, neutral_brief)
        boot_prompt_file = None
        if cwd:
            # Keep tmux's new-session command small. Codex's interactive TUI accepts only a
            # positional prompt; generated launch packets can be tens of KB, and passing that text
            # through tmux trips "command too long". The pane starts a tiny loader, reads the prompt
            # from disk inside the node workspace, then execs Codex with that prompt as its first
            # turn.
            boot_prompt_file = Path(cwd) / CODEX_BOOT_PROMPT_FILE
            boot_prompt_file.write_text(boot_prompt, encoding="utf-8")
            argv = [
                "/bin/sh",
                "-c",
                _BOOT_PROMPT_LOADER,
                "codex-boot",
                str(boot_prompt_file),
                *codex_argv,
            ]
            recorded_argv = [*codex_argv, str(boot_prompt_file)]
        else:
            # Bare adapter-level dry-runs without a workspace keep the historical direct prompt
            # shape. Production spawns have a workspace.
            argv = [*codex_argv, boot_prompt]
            recorded_argv = list(argv)

        # (3) The pane env floor — CODEX-OWN, never CC's. The chokepoint hands every adapter the
        # commissioned CC env (CLAUDE_CODE_OAUTH_TOKEN + config dir + kill-switches): STRIP all
        # CLAUDE_* vars (a CC OAuth token must never ride a codex pane — cross-runtime credential
        # hygiene; also drops the dry-run placeholder sentinels the transport rightly refuses)
        # and build the probed floor: PATH (codex's npm shim needs node; shells it spawns need
        # standard bins + homebrew), HOME, TERM, and a per-worker CODEX_HOME. The canonical
        # pinned home owns refresh; the worker home has isolated mutable state and no usable
        # refresh token. Auth is file-based, never an env credential (the negative gate below
        # still enforces).
        env = {k: v for k, v in env.items() if not k.startswith("CLAUDE_") and k != "DISABLE_AUTOUPDATER"}
        env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin")
        env.setdefault("HOME", os.path.expanduser("~"))
        env.setdefault("TERM", "xterm-256color")

        # (4) Preflight canonical OAuth, then seed the per-worker Codex home. The preflight
        # catches expired/missing login before any pane opens; the child home gives each
        # concurrent L5 isolated sqlite/log/session/history state and no usable refresh token.
        try:
            notify_argv = None
            if (
                _brief_get("turn_hook_profile") == turn_state.CODEX_TURN_END_ONLY
                and _brief_get("turn_runtime_root")
            ):
                hook_owner_token = turn_state.hook_owner_token(
                    runtime_root=_brief_get("turn_runtime_root"),
                    node_address=tmux_target,
                )
                notify_argv = turn_state.hook_argv(
                    python_executable=sys.executable,
                    runtime_root=_brief_get("turn_runtime_root"),
                    node_address=tmux_target,
                    owner_token=hook_owner_token,
                    runtime="codex",
                )
            seat = codex_auth.seed_ephemeral_home(
                _pinned_home(),
                node_address=tmux_target,
                trust_cwd=cwd,
                reasoning_effort=level_config.reasoning_effort,
                notify_argv=notify_argv,
            )
        except oauth_guard.ApiKeyForbidden:
            raise
        except SpawnFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - auth home I/O is a spawn-time runtime failure.
            raise SpawnFailure(
                f"could not prepare codex worker home: {exc}",
                failure_class="runtime_down",
            ) from exc
        worker_home = seat.home
        env["CODEX_HOME"] = str(worker_home)
        ca_bundle = _prepare_adguard_ca_bundle(worker_home)
        if ca_bundle is not None:
            env["CODEX_CA_CERTIFICATE"] = str(ca_bundle)
        if containment is not None:
            containment = sandbox.finalize_runtime_state(
                containment,
                runtime="codex",
                state_root=str(worker_home),
            )

        # (5) The from-empty pane + the shared OAuth-only negative gate BEFORE the actor opens.
        pane_argv = self.tmux.build_pane_argv(env, argv)
        oauth_guard.assert_oauth_only(env, recorded_argv, pane_argv, self.tmux.server_env())

        # (5a) Apply the same external seatbelt profile as Claude.  Enforce refuses before actor
        # open when SBPL cannot apply.  Observe may continue only with a durable degraded posture.
        session_name = addressing.session_name_for(tmux_target)
        launch_argv = pane_argv
        containment_posture = None
        if containment is not None:
            try:
                prepared = sandbox.prepare_launch(
                    pane_argv,
                    containment,
                    base_dir=str(worker_home),
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
                # The argv already carries the bypass flag (it is fixed before the profile is
                # rendered), so an unapplied profile would open a Codex seat with NEITHER sandbox —
                # its own is off and ours never took. SECURITY.md constraint 4 couples the bypass to
                # the jail ("skip-permissions INSIDE the jail … containment bounds the blast
                # radius"), so the coupling's plain consequence is: no jail, no open. Observe's
                # continue-degraded allowance covers a seat that merely loses read REPORTING, not
                # one that loses its whole containment. Enforce already raises inside prepare_launch.
                raise SpawnFailure(
                    "codex seat carries the externally-sandboxed bypass but the harness seatbelt "
                    f"did not apply: {prepared.posture.get('degraded_reason') or 'unknown reason'}",
                    failure_class="containment_apply_failed",
                )

        # (6) Open the detached actor (the F18 canonical-target contract).
        boot_epoch = time.time()
        if cwd:
            canonical_target = self.tmux.create_detached(session_name, launch_argv, env, cwd=cwd)
        else:
            canonical_target = self.tmux.create_detached(session_name, launch_argv, env)

        # (7) Discover the rollout (session uuid + transcript path) — the REAL files codex
        # writes, never an invented uuid.
        sessions_root = worker_home / "sessions"
        session_uuid, transcript_path = self.discover_rollout(
            sessions_root, cwd or os.path.realpath(os.getcwd()), boot_epoch
        )
        self.fail_on_rollout_auth_error(transcript_path)

        return SpawnResult(
            ok=True,
            session_uuid=session_uuid,
            model_used=f"{getattr(level_config, 'model', '?')} / codex",
            role_variant=role_variant,
            system_prompt_file=CODEX_NATIVE_INSTRUCTIONS,
            system_prompt_file_hash=hashlib.sha256(
                CODEX_NATIVE_INSTRUCTIONS.encode("utf-8")
            ).hexdigest(),
            tmux_target=canonical_target,
            transcript_path=transcript_path,
            failure_class=None,
            argv=tuple(recorded_argv),
            env=dict(env),
            permission_posture=permission_posture,
            containment_posture=containment_posture,
            codex_seat_id=seat.seat_id,
            codex_auth_version=seat.auth_version,
            codex_access_seconds_remaining=seat.access_seconds_remaining,
            launch_packet_file=_brief_get("launch_packet_file"),
            launch_packet_hash=_brief_get("launch_packet_hash"),
            launch_surface_source_hash=_brief_get("launch_surface_source_hash"),
            reference_map_file=_brief_get("reference_map_file"),
            reference_map_hash=_brief_get("reference_map_hash"),
            reference_map_json_file=_brief_get("reference_map_json_file"),
            launch_surface_version=_brief_get("launch_surface_version"),
        )
