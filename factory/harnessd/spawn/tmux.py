"""tmux — the thin REAL tmux control-plane wrapper (IMPLEMENTATION-PLAN §2.11; DAEMON §6.2).

The transport the spawn chokepoint, detector, and reconcile sweep talk through. Unlike
``oauth_guard`` (pure functions) this module shells out to a REAL ``tmux`` binary via
``subprocess`` — it is the boundary between the harness and the OS process tree.

The §2.11 frozen surface (return contract REVISED by F18 / finding OSA-01):

  * ``create_detached(session_name, argv, env) -> canonical_target`` — open a DETACHED session
    whose pane process is the FROM-EMPTY isolator ``env -i <K=V…> <argv…>`` (``build_pane_argv``).
    ``env -i`` clears the inherited environment and re-adds ONLY the explicit vars, so the
    pane inherits NOTHING from the long-lived tmux SERVER env (the load-bearing OAuth-only
    mechanism: a launchd-spawned daemon may have widened the server env with a stray
    ANTHROPIC_API_KEY/OPENAI_API_KEY; a bare ``new-session <cmd>`` would leak it into the pane).
    Returns the CANONICAL live target ``<session>:<window>.<pane>`` exactly as tmux reports it
    (``-P -F '#{session_name}:#{window_index}.#{pane_index}'``) — the post-rename truth (tmux
    3.6a silently rewrites ':'/'.' in session names to '_') with the REAL indices (a non-zero
    base-index/pane-base-index config is reported correctly, never guessed). This return IS a
    ``list_targets()`` key, so it is the ONE value the adapter records as ``tmux_target``.
  * ``capture_pane(session_name) -> str`` — ``capture-pane -p`` readback of the pane buffer.
  * ``list_targets() -> {tmux_target: {pane_pid, pane_dead, window_activity}}`` — the live
    control-plane snapshot Increments 6/7 (detector / reconcile) read.
  * ``kill(session_name) -> None`` — tear a session down (the control-plane teardown edge).
  * ``server_env() -> dict`` — ``show-environment -g`` readback: WHAT THE SERVER WOULD LEAK,
    the input ``oauth_guard.assert_pane_env_isolated`` checks before a spawn.

THE SOCKET SEAM (``set_socket`` / module-level ``_SOCKET``):
    Every tmux invocation is prefixed ``tmux -L <socket>`` when a socket is bound. The daemon
    binds ONE dedicated socket for its server; tests bind a unique per-test ``-L`` socket so a
    real-tmux test drives an ISOLATED server (never the user's default) and a kill-server
    teardown guarantees no leak. ``set_socket(None)`` falls back to the default tmux server.

DESIGN DECISIONS for §2.11 details the plan leaves to the builder (stated in the build report):
  * ``build_pane_argv`` is the SINGLE from-empty pane constructor BOTH this wrapper's
    ``create_detached`` and the Claude adapter use, so the guard and the wrapper agree on the
    exact isolator shape (the test pins ``build_pane_argv(...)[: 2] == ["env", "-i"]``).
  * ``list_targets`` keys are ``<session_name>:<window_index>.<pane_index>`` (the tmux target
    triple) — ``window_activity`` is included per the §2.11 shape even though v1's verdict fuses
    only ``pane_pid``/``pane_dead`` (Increment 6).
  * ``new-session`` receives ``argv`` as DISTINCT argv tokens (NOT a re-quoted shell string), so
    the ``env -i <K=V…> <argv…>`` vector is handed to tmux verbatim — no quoting round-trip that
    could re-expand a value.
"""

from __future__ import annotations

import os
import subprocess
import threading

# ---------------------------------------------------------------------------
# The socket seam. None -> the user's default tmux server. A bound socket ->
# `tmux -L <socket>` (a DEDICATED server: the daemon's, or a per-test isolated one).
# ---------------------------------------------------------------------------

_SOCKET: str | None = None
_KEYSTROKE_LOCK = threading.Lock()


def set_socket(socket: str | None) -> None:
    """Bind (or clear) the dedicated ``-L`` socket every tmux invocation targets.

    The daemon binds its one server socket once at boot; tests bind a unique per-test socket
    so they drive an ISOLATED server (and tear it down via kill-server) without ever touching
    the user's default tmux server. ``set_socket(None)`` restores the default-server behavior.
    """
    global _SOCKET
    _SOCKET = socket


def _base_cmd() -> list[str]:
    """The ``tmux [-L <socket>]`` prefix every invocation is built on."""
    cmd = [os.environ.get("HARNESSD_TMUX", "tmux")]
    if _SOCKET is not None:
        cmd += ["-L", _SOCKET]
    return cmd


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a tmux subcommand on the bound socket, capturing output (text)."""
    return subprocess.run(
        _base_cmd() + args,
        check=check,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# The from-empty pane isolator — the SINGLE constructor the wrapper AND the adapter share.
# ---------------------------------------------------------------------------

def build_pane_argv(env: dict, argv: list[str]) -> list[str]:
    """Build the from-empty ``env -i <K=V…> <argv…>`` pane command (§2.11, the OAuth-only floor).

    ``env -i`` clears the inherited environment, then re-adds ONLY the explicit ``env`` vars, so
    the pane inherits NOTHING from the tmux SERVER environment. This is the ONE place the
    isolator vector is assembled — the adapter calls this same seam so the guard
    (``oauth_guard.assert_pane_env_isolated``, which requires ``pane_argv[:2] == ["env", "-i"]``)
    and the wrapper agree on the exact pane shape.
    """
    pane = ["env", "-i"]
    for key, value in env.items():
        pane.append(f"{key}={value}")
    pane += list(argv)
    return pane


# ---------------------------------------------------------------------------
# create_detached — open a detached session whose pane is the from-empty isolator.
# ---------------------------------------------------------------------------

def create_detached(session_name: str, argv: list[str], env: dict, cwd: str | None = None) -> str:
    """Open a DETACHED tmux session running the from-empty ``env -i`` pane; return the
    CANONICAL live target ``<session>:<window>.<pane>`` (F18 / finding OSA-01).

    The pane command is ``build_pane_argv(env, argv)`` — ``env -i <K=V…> <argv…>`` — handed to
    ``new-session`` as distinct argv tokens. ``-d`` keeps the session detached (the daemon owns
    it, not an attached terminal). ``cwd`` (optional) boots the pane in that directory
    (``-c <cwd>``) — the adapter passes the node's workspace so the agent starts where its
    brief lands and its relative reads agree with the kickoff pointer.

    THE RETURN CONTRACT (revised by F18): tmux ITSELF reports the created target via
    ``-P -F '#{session_name}:#{window_index}.#{pane_index}'`` — never an echo of the requested
    name. tmux 3.6a silently RENAMES session names containing ':' or '.' (to '_' variants), and
    a user ``base-index``/``pane-base-index`` shifts the window/pane indices — so the only
    trustworthy key is the one tmux prints back. The return is byte-for-byte a ``list_targets()``
    key; the adapter records it as the binding's ``tmux_target`` so pane_alive / the reconcile
    sweep / send-keys all address the pane that actually exists.

    WRAPPING-IDEMPOTENT: if ``argv`` ALREADY begins with the from-empty isolator (``env -i``) —
    the adapter pre-builds the exact pane vector it gates with ``build_pane_argv`` and passes it
    here — it is used verbatim (NOT re-wrapped). A raw ``argv`` (the direct-call path the
    real-tmux tests use, e.g. ``["sh", "-c", …]``) is wrapped from-empty. Either way the pane is
    a single from-empty ``env -i`` isolator.

    THE PLACEHOLDER REFUSAL (LT-1): this is the REAL transport — a pane launched with the
    structural placeholder token (``chokepoint._spawn_env``'s ``<oauth-token-file>`` sentinel,
    the dry-run shape that means "the daemon never bound the commissioned env") would boot an
    UNAUTHENTICATED CC on a nonexistent config dir and freeze on first-boot dialogs the kickoff's
    Enter then answers. Refuse it LOUDLY here (a ``SpawnFailure`` the chokepoint's §6.3 net
    catches: claim released + spawn_failed escalation) — mock transports never reach this code,
    so the structural tests keep their placeholder shape.
    """
    from harnessd.spawn import oauth_guard as _oauth_guard

    if isinstance(env, dict) and env.get("CLAUDE_CODE_OAUTH_TOKEN") == _oauth_guard.PLACEHOLDER_OAUTH_TOKEN:
        raise _oauth_guard.SpawnFailure(
            "REFUSING a real create_detached with the structural PLACEHOLDER env "
            "(CLAUDE_CODE_OAUTH_TOKEN='<oauth-token-file>'): the daemon never bound the "
            "commissioned spawn env (chokepoint.set_spawn_env — LT-1). A pane launched this way "
            "boots an unauthenticated CC on an unseeded config dir.",
            failure_class="placeholder_env",
        )
    if list(argv[:2]) == ["env", "-i"]:
        pane_argv = list(argv)
    else:
        pane_argv = build_pane_argv(env, argv)
    # `new-session -d -s <session> [-c <cwd>] -P -F '#{session_name}:#{window_index}.#{pane_index}'`
    # opens detached and prints the CANONICAL target triple (the post-rename session name +
    # the real indices). The pane_argv tokens follow as the command (NOT a re-quoted string).
    args = ["new-session", "-d", "-s", session_name]
    if cwd:
        # The pane boots IN the node's workspace (-c) so the agent's relative reads (brief.md,
        # .inbox.<seat>.jsonl) agree with the kickoff pointer — and the trust seed covers this dir.
        args += ["-c", str(cwd)]
    args += ["-P", "-F", "#{session_name}:#{window_index}.#{pane_index}"] + pane_argv
    # LT-4/INT-1: a failed new-session must NOT escape as a raw CalledProcessError — the
    # chokepoint's §6.3 net catches only (SpawnFailure, ApiKeyForbidden), so an uncaught duplicate-
    # session crash would LEAK the committed claim and kill genesis/IPC unstructured. Convert it:
    # 'duplicate session' (the deterministic per-address name colliding with a still-live prior
    # pane) -> failure_class='tmux_session_collision'; anything else -> 'runtime_down'. Either way
    # the claim releases and the spawn_failed escalation names the class.
    try:
        proc = _run(args)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if "duplicate session" in stderr:
            raise _oauth_guard.SpawnFailure(
                f"tmux session collision: a live session already holds {session_name!r} "
                f"(a prior incarnation's pane was never torn down): {stderr}",
                failure_class="tmux_session_collision",
            ) from exc
        raise _oauth_guard.SpawnFailure(
            f"tmux new-session failed (rc={exc.returncode}): {stderr or exc}",
            failure_class="runtime_down",
        ) from exc
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# send_keys — the keystroke delivery channel (the transport increment).
# ---------------------------------------------------------------------------

def send_keys(target: str, text: str) -> bool:
    """Type ``text`` LITERALLY into the pane, then Enter — the one keystroke-delivery seam.

    Two send-keys calls, mirroring the proven interactive_eval precedent (eval/interactive_eval
    Pane.send_text): first the literal text (``-l`` — no key-name lookup, no shell expansion;
    the pointer string is delivered byte-for-byte), a short settle pause (Claude Code's input
    box needs the text rendered before the submit), then ``Enter`` to submit.

    ``target`` accepts the canonical ``<session>:<window>.<pane>`` triple ``create_detached``
    returns (the recorded ``tmux_target``) or a bare session name.

    RETURNS True iff BOTH sends exited 0 (LT-2): a dead/missing target ('no such session') exits
    non-zero, and swallowing that rc made a failed send indistinguishable from a delivered one —
    the wake watermark advanced on undelivered nudges and the ``*_send_failed`` journal rows were
    unreachable with the real transport. The send stays non-raising (check=False — still no tmux
    ACK that the AGENT saw it; the §3.2-P3 verify-new-turn discipline remains the only true
    confirmation), but delivery-level failure now surfaces so the caller journals it and leaves
    the watermark unmoved (the next tick retries).
    """
    import time as _time

    with _KEYSTROKE_LOCK:
        first = _run(["send-keys", "-t", target, "-l", text], check=False)
        _time.sleep(0.3)  # let the input box render the literal before submitting (eval precedent)
        second = _run(["send-keys", "-t", target, "Enter"], check=False)
    return first.returncode == 0 and second.returncode == 0


def send_cancel(target: str) -> bool:
    """Send exactly one capability-reducing Escape, exclusive with every text+Enter send."""
    with _KEYSTROKE_LOCK:
        result = _run(["send-keys", "-t", target, "Escape"], check=False)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# capture_pane — readback of the pane's visible buffer.
# ---------------------------------------------------------------------------

def capture_pane(session_name: str) -> str:
    """Return the pane's captured buffer (``capture-pane -p``) — the pane-readback channel.

    ``-t`` accepts EITHER a bare session name OR the full canonical ``<session>:<window>.<pane>``
    triple ``create_detached`` returns (the recorded ``tmux_target``) — tmux resolves both; the
    triple is exact. ``-p`` prints the buffer to stdout. Used to read back a fake-CLI pane's
    output in the real-tmux tests (e.g. the ``printenv`` isolation probe) and by the watchdog's
    prod-gate capture.
    """
    proc = _run(["capture-pane", "-p", "-t", session_name], check=False)
    return proc.stdout


# ---------------------------------------------------------------------------
# list_targets — the live control-plane snapshot (the §2.11 shape Inc 6/7 read).
# ---------------------------------------------------------------------------

def list_targets() -> dict[str, dict]:
    """Return ``{<session>:<window>.<pane> -> {pane_pid, pane_dead, window_activity}}`` (§2.11).

    A ``list-panes -a`` sweep across the WHOLE server, formatted into the exact shape Increment
    6's ``pane_alive`` (``targets[t]["pane_dead"]`` / ``["pane_pid"]``) and Increment 7's
    reconcile sweep read. ``pane_pid`` is a real int; ``pane_dead`` is 0/1. On an empty server
    (no sessions) tmux exits non-zero with "no server running" — that is the empty snapshot, so
    we return ``{}`` rather than raising.
    """
    fmt = "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_pid}\t#{pane_dead}\t#{window_activity}"
    proc = _run(["list-panes", "-a", "-F", fmt], check=False)
    if proc.returncode != 0:
        # No server / no sessions -> an empty snapshot, not an error.
        return {}
    targets: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        session, window, pane, pid, dead, activity = parts[:6]
        target = f"{session}:{window}.{pane}"
        targets[target] = {
            "pane_pid": int(pid) if pid.isdigit() else None,
            "pane_dead": int(dead) if dead.isdigit() else dead,
            "window_activity": int(activity) if activity.lstrip("-").isdigit() else activity,
        }
    return targets


# ---------------------------------------------------------------------------
# kill — control-plane teardown of a session.
# ---------------------------------------------------------------------------

def kill(session_name: str) -> None:
    """Kill a session (``kill-session -t``) — the control-plane teardown edge.

    ``-t`` accepts EITHER a bare session name OR the full canonical ``<session>:<window>.<pane>``
    triple ``create_detached`` returns (the recorded ``tmux_target``) — kill-session resolves the
    pane spec to its owning session, so killing by the recorded target tears the session down.
    Best-effort: a session that is already gone is not an error here (idempotent teardown).
    Per §2.11 the chokepoint reaches this ONLY via the executor-stamping path for control
    state; the raw transport is idempotent so reconcile/teardown can call it safely.
    """
    _run(["kill-session", "-t", session_name], check=False)


# ---------------------------------------------------------------------------
# server_env — what the tmux SERVER environment would leak (the guard's input).
# ---------------------------------------------------------------------------

def server_env() -> dict:
    """Return the tmux SERVER global environment (``show-environment -g``) as a dict (§2.11).

    This surfaces WHAT THE SERVER WOULD LEAK into a pane — the input
    ``oauth_guard.assert_pane_env_isolated`` checks to refuse a spawn from a server that already
    carries a stray ANTHROPIC_API_KEY/OPENAI_API_KEY. ``show-environment -g`` prints ``K=V`` per
    line; a ``-K`` line means the var is REMOVED from the server env (we drop those). On a server
    with no sessions yet tmux may report none — an empty dict is the clean snapshot.
    """
    proc = _run(["show-environment", "-g"], check=False)
    if proc.returncode != 0:
        return {}
    env: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line or line.startswith("-"):
            # `-NAME` => the variable is to be UNSET in the pane; not a leakable value.
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env
