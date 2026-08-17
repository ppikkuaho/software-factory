"""Increment 9 — FROZEN acceptance for harnessd/spawn/tmux.py (the REAL tmux wrapper).

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.11 — the FROZEN tmux.py interface (create_detached / capture_pane /
    list_targets / kill / server_env). The CRITICAL OAuth-only mechanism: the pane command is
    `env -i <K=V …> <argv…>` so the pane is built FROM-EMPTY and inherits NOTHING from the tmux
    SERVER env. NEVER a bare inherited-env new-session.
  * IMPLEMENTATION-PLAN §7 + the Increment-9 Done-test parts (b) and (c).
  * DAEMON §6.2 (the H40 boot recipe: detached session, env-isolation, pane-capture readback).

This is the FIRST real-tmux increment (Lesson 7 — BIAS TO REAL): parts (b)/(c) drive a REAL local
tmux server on a DEDICATED socket (`tmux -L <unique>`), with a FAKE CLI as the pane process (a shell
command that sleeps / prints — ZERO model burn, NO claude binary). A teardown fixture kills the server
so no sessions/servers leak. Unique session names per test.

The real-tmux tests are guarded by `pytest.mark.skipif(shutil.which("tmux") is None)` so the suite
still COLLECTS on a tmux-less CI — but where tmux is installed (3.6a here) they RUN, not skip.

NO IMPLEMENTATION here — harnessd/spawn/tmux.py does not exist yet (RED first).

Load-bearing properties (each pins a mutant from the plan's mutant hints):
  * (b) env -i REALLY isolates: a server carrying ANTHROPIC_API_KEY does NOT leak it into an
        env-i pane (mutant: bare new-session inheriting the server env -> the real capture-pane sees
        the leaked key -> caught).
  * (b) assert_pane_env_isolated RAISES when pane_argv lacks the env -i wrapper (mutant: skip the
        isolator check -> caught) AND when the real server_env carries a forbidden key.
  * (c) list_targets returns the {pane_pid, pane_dead, window_activity} shape Inc 6/7 read; a real
        living pane has a real int pane_pid and pane_dead == 0 (mutant: wrong shape -> caught).
"""

from __future__ import annotations

import importlib
import os
import shutil
import uuid

import pytest

from harnessd.spawn import oauth_guard


_HAS_TMUX = shutil.which("tmux") is not None
_real_tmux = pytest.mark.skipif(not _HAS_TMUX, reason="real-tmux test: tmux binary not installed")


def _tmux_mod():
    return importlib.import_module("harnessd.spawn.tmux")


def _rm_socket_file(sock):
    """Remove the dedicated -L socket FILE after kill-server, so no socket leaks on disk.

    tmux puts per-user sockets under $TMPDIR/tmux-<uid>/ (and the /private/tmp twin on macOS).
    kill-server stops the SERVER but can leave the socket file; this sweeps it. Best-effort.
    """
    uid = os.getuid()
    candidates = []
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        candidates.append(os.path.join(tmpdir, f"tmux-{uid}", sock))
    candidates.append(os.path.join("/tmp", f"tmux-{uid}", sock))
    candidates.append(os.path.join("/private/tmp", f"tmux-{uid}", sock))
    for path in candidates:
        try:
            os.unlink(path)
        except OSError:
            pass


# ===========================================================================
# Module + interface presence (RED-until-built).
# ===========================================================================

def test_tmux_module_exposes_frozen_interface():
    """The §2.11 frozen surface exists: create_detached/capture_pane/list_targets/kill/server_env."""
    mod = _tmux_mod()
    for name in ("create_detached", "capture_pane", "list_targets", "kill", "server_env"):
        assert hasattr(mod, name), f"tmux.py must expose {name} (§2.11 frozen interface)"
        assert callable(getattr(mod, name)), f"tmux.{name} must be callable"


# ===========================================================================
# REAL-TMUX fixture — dedicated socket + kill-server teardown so nothing leaks.
# ===========================================================================

@pytest.fixture
def tmux_server():
    """A REAL tmux server on a DEDICATED unique -L socket, torn down via kill-server.

    Binds the tmux module's wrapper to this socket (the module exposes a `set_socket`
    seam so the same code path the daemon uses talks to an isolated server here). The
    fixture NEVER touches the user's default tmux server, and kill-server in teardown
    guarantees no leaked sessions/servers. The fake pane process is a shell command —
    ZERO model burn.
    """
    sock = "harness-test-" + uuid.uuid4().hex[:12]
    mod = _tmux_mod()
    # The wrapper MUST provide a way to target a dedicated socket (so tests cannot leak
    # into / clobber the user's default tmux). If the impl named it differently, the
    # frozen contract still requires a per-test isolated server — fail loud here.
    assert hasattr(mod, "set_socket"), (
        "tmux.py must expose a socket seam (set_socket) so tests drive a DEDICATED "
        "`tmux -L <socket>` server and never the user's default server"
    )
    prior = getattr(mod, "_SOCKET", None)
    mod.set_socket(sock)
    try:
        yield mod, sock
    finally:
        # kill the dedicated server unconditionally (teardown — no leaked server).
        subprocess = importlib.import_module("subprocess")
        subprocess.run(["tmux", "-L", sock, "kill-server"], check=False,
                       capture_output=True)
        _rm_socket_file(sock)  # also remove the stale socket FILE (no leak on disk)
        mod.set_socket(prior)


def _fake_cli(body: str) -> list[str]:
    """A FAKE CLI pane process: a shell command, NOT the claude binary (zero model burn)."""
    return ["sh", "-c", body]


def _session_name() -> str:
    return "harness-sess-" + uuid.uuid4().hex[:8]


# ===========================================================================
# Part (b) — the OAuth-only BLOCKING test: REAL tmux pane-env leak.
#   Start a tmux server WITH ANTHROPIC_API_KEY in the SERVER env, spawn an env-i
#   pane via create_detached, and assert capture_pane of `printenv ANTHROPIC_API_KEY`
#   is EMPTY (the from-empty isolation works against a REAL leaky server).
# ===========================================================================

@_real_tmux
def test_create_detached_env_i_isolates_pane_from_leaky_server(tmux_server, monkeypatch):
    """REAL leaky server + env-i pane -> the pane does NOT see the server's ANTHROPIC_API_KEY.

    This is the load-bearing OAuth-only mechanism. We put ANTHROPIC_API_KEY into the ENV that
    starts the tmux SERVER (the launchd-daemon-leaks-a-stray-key scenario), spawn a pane whose
    command prints ANTHROPIC_API_KEY, and assert the captured output carries the key NOWHERE —
    only our explicit SENTINEL, proving `env -i` cleared the inherited environment.
    """
    mod, _sock = tmux_server
    # Make the SERVER environment carry the forbidden key: the FIRST tmux command on this
    # socket starts the server and snapshots this process env into it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "LEAK_ME_SERVER")

    session = _session_name()
    # The 4-var isolation env the adapter would assemble (no API key in it).
    env = {
        "CLAUDE_CONFIG_DIR": "/tmp/x",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-xyz",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    # FAKE CLI: print the (hopefully empty) leaked key, then a sentinel, then idle so the pane lives.
    argv = _fake_cli("printenv ANTHROPIC_API_KEY; echo SENTINEL_ISOLATED; sleep 30")

    pane_id = mod.create_detached(session, argv, env)
    assert isinstance(pane_id, str) and pane_id, "create_detached must return a non-empty pane_id"

    # readback channel: capture-pane -p
    import time
    captured = ""
    for _ in range(40):
        captured = mod.capture_pane(session)
        if "SENTINEL_ISOLATED" in captured:
            break
        time.sleep(0.1)

    assert "SENTINEL_ISOLATED" in captured, (
        "the fake pane process must have run (sentinel present) — proves we captured the right pane"
    )
    assert "LEAK_ME_SERVER" not in captured, (
        "env -i isolation FAILED: the pane saw the tmux SERVER's ANTHROPIC_API_KEY. The pane must be "
        "built from-empty (`env -i`), inheriting NOTHING from the server env (the load-bearing "
        "OAuth-only mechanism, §2.11). A bare inherited-env new-session would leak it here."
    )

    mod.kill(session)


@_real_tmux
def test_create_detached_pane_argv_begins_with_env_i(tmux_server, monkeypatch):
    """Belt-and-suspenders: the ACTUAL pane process is an `env -i …` from-empty isolator.

    The pane's argv[0..1] is `env -i` (not a bare command, not `env` without `-i`). We verify by
    running a sentinel command and confirming the pane env is empty-except-our-vars (FOO unset
    from the server, our CLAUDE_CONFIG_DIR present), which only holds under `env -i`.
    """
    mod, _sock = tmux_server
    monkeypatch.setenv("STRAY_SERVER_VAR", "should_not_leak")

    session = _session_name()
    env = {
        "CLAUDE_CONFIG_DIR": "/tmp/cfg",
        "CLAUDE_CODE_OAUTH_TOKEN": "tok",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    argv = _fake_cli(
        "printenv STRAY_SERVER_VAR; echo ---; printenv CLAUDE_CONFIG_DIR; echo SENT_END; sleep 30"
    )
    mod.create_detached(session, argv, env)

    import time
    cap = ""
    for _ in range(40):
        cap = mod.capture_pane(session)
        if "SENT_END" in cap:
            break
        time.sleep(0.1)

    assert "SENT_END" in cap
    assert "should_not_leak" not in cap, (
        "a stray SERVER var leaked into the pane — env -i did not clear the inherited env"
    )
    assert "/tmp/cfg" in cap, "the 4-var isolation env's CLAUDE_CONFIG_DIR must be present in the pane"
    mod.kill(session)


@_real_tmux
def test_server_env_reads_back_the_leaked_key(tmux_server, monkeypatch):
    """server_env() surfaces what the SERVER would leak — the input the guard checks.

    With ANTHROPIC_API_KEY in the env that started the server, server_env() (a
    `show-environment -g` readback) MUST report it, so the pane-isolation guard has a real
    signal to refuse a leaky server.
    """
    mod, _sock = tmux_server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "LEAK_ME_SHOWENV")
    # touch the server so it exists with the env snapshot
    session = _session_name()
    mod.create_detached(session, _fake_cli("sleep 30"), {"CLAUDE_CONFIG_DIR": "/x"})

    senv = mod.server_env()
    assert isinstance(senv, dict)
    assert senv.get("ANTHROPIC_API_KEY") == "LEAK_ME_SHOWENV", (
        "server_env() must read back the SERVER's global environment (show-environment -g) so the "
        "pane-isolation guard can refuse a leaky server (§2.11)"
    )
    mod.kill(session)


def test_assert_pane_env_isolated_raises_without_env_i_against_real_server_env():
    """The pane-isolation guard RAISES when pane_argv lacks the env -i wrapper.

    This is the guard half of the blocking test — a mutant adapter that builds a BARE
    new-session pane (no `env -i`) is caught by oauth_guard.assert_pane_env_isolated. The
    POSITIVE half is anchored to the REAL tmux wrapper's own pane construction (the
    `build_pane_argv` seam the §2.11 `create_detached` uses), so this gates on Increment-9
    being built — not just on the already-shipped Increment-8 guard.
    """
    mod = _tmux_mod()  # RED until harnessd/spawn/tmux.py exists
    bare_pane = ["sh", "-c", "claude --system-prompt-file operational/shared/system-prompt.md"]
    with pytest.raises(oauth_guard.ApiKeyForbidden):
        oauth_guard.assert_pane_env_isolated(bare_pane, server_env={})

    # the REAL wrapper's from-empty isolator (env -i <K=V…> <argv…>) passes against a clean server.
    assert hasattr(mod, "build_pane_argv"), (
        "tmux.py must expose build_pane_argv(env, argv) — the single from-empty `env -i` pane "
        "constructor create_detached uses (so the guard and the wrapper agree on the same isolator)"
    )
    iso_pane = mod.build_pane_argv(
        {"CLAUDE_CONFIG_DIR": "/x"}, ["claude", "--system-prompt-file", "operational/shared/system-prompt.md"]
    )
    assert iso_pane[:2] == ["env", "-i"], "build_pane_argv must produce a from-empty `env -i` pane"
    assert oauth_guard.assert_pane_env_isolated(iso_pane, server_env={}) is None


@_real_tmux
def test_isolated_pane_still_refused_when_real_server_env_leaks(tmux_server, monkeypatch):
    """Even an env-i pane is refused if the REAL server_env carries a forbidden key.

    Closes the second half of the blocking test: assert_pane_env_isolated reads the REAL
    server_env (from a leaky live server) and RAISES, because a clean 4-var dict cannot
    redeem a server that already leaks a key the pane *could* inherit from.
    """
    mod, _sock = tmux_server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "LEAK_ME_GUARD")
    session = _session_name()
    mod.create_detached(session, _fake_cli("sleep 30"), {"CLAUDE_CONFIG_DIR": "/x"})

    iso_pane = ["env", "-i", "CLAUDE_CONFIG_DIR=/x", "claude"]
    with pytest.raises(oauth_guard.ApiKeyForbidden):
        oauth_guard.assert_pane_env_isolated(iso_pane, server_env=mod.server_env())
    mod.kill(session)


# ===========================================================================
# Part (c) — tmux-interface conformance: list_targets shape.
#   {tmux_target: {pane_pid, pane_dead, window_activity}} — the §2.11 shape Inc 6/7 read.
# ===========================================================================

@_real_tmux
def test_list_targets_returns_inc6_shape_for_a_living_pane(tmux_server):
    """list_targets returns {pane_pid:int, pane_dead, window_activity} for a real living pane.

    pane_pid is a REAL int; pane_dead == 0 for an alive pane (the exact shape Increment 6's
    pane_alive reads: `targets.get(target)["pane_dead"]` / `["pane_pid"]`).
    """
    mod, _sock = tmux_server
    session = _session_name()
    mod.create_detached(session, _fake_cli("sleep 30"), {"CLAUDE_CONFIG_DIR": "/x"})

    import time
    targets = {}
    target_key = None
    for _ in range(40):
        targets = mod.list_targets()
        # find the pane belonging to our session
        for k in targets:
            if k.startswith(session + ":"):
                target_key = k
                break
        if target_key:
            break
        time.sleep(0.1)

    assert target_key is not None, (
        f"list_targets must include a target for session {session!r}; got keys {list(targets)!r}"
    )
    info = targets[target_key]
    assert set(("pane_pid", "pane_dead", "window_activity")).issubset(info.keys()), (
        f"list_targets target value must carry the {{pane_pid, pane_dead, window_activity}} shape "
        f"Increments 6/7 read; got keys {sorted(info)!r}"
    )
    assert isinstance(info["pane_pid"], int) and info["pane_pid"] > 0, (
        "pane_pid must be a real positive int (the live process pid)"
    )
    assert int(info["pane_dead"]) == 0, "a living pane must report pane_dead == 0"
    mod.kill(session)


@_real_tmux
def test_kill_removes_the_target(tmux_server):
    """kill(session) removes the session's target from list_targets (control-plane teardown)."""
    mod, _sock = tmux_server
    session = _session_name()
    mod.create_detached(session, _fake_cli("sleep 30"), {"CLAUDE_CONFIG_DIR": "/x"})

    import time
    present = False
    for _ in range(40):
        if any(k.startswith(session + ":") for k in mod.list_targets()):
            present = True
            break
        time.sleep(0.1)
    assert present, "the spawned session must appear in list_targets before kill"

    mod.kill(session)
    # after kill the target is gone
    gone = False
    for _ in range(40):
        if not any(k.startswith(session + ":") for k in mod.list_targets()):
            gone = True
            break
        time.sleep(0.1)
    assert gone, "after kill(session) the target must be absent from list_targets"
