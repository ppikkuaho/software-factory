"""Increment 9 — part (d): MOCK-CONTRACT VALIDATION (pin the Inc-6 detector mock to reality).

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §7 — the Increment-9 Done-test part (d): run the REAL detector_signals
    against the REAL tmux boundary (NOT the Inc-6 mock). pane_alive on a real LIVING pane ->
    (True, <real pid>); on a real KILLED/closed pane -> (False / pane_dead); jsonl_progress ->
    grew=True for a real transcript appended-to between calls, grew=False for a flat one.
  * detector_signals.py §"THE TMUX SEAM" — pane_alive() reaches tmux ONLY through the module-level
    `_tmux` reference; Increment 9 binds `_tmux = harnessd.spawn.tmux`. This test does exactly that
    (and restores it after), proving real tmux emits the {pane_pid, pane_dead, window_activity} shape
    Increment 6 was MOCKED against — so the two cannot silently drift (Lesson 6).

REAL tmux server + FAKE CLI pane (sh -c, sleeps / appends to a JSONL) — ZERO model burn, NO claude
binary. Dedicated -L socket + kill-server teardown fixture so nothing leaks. Skipped (not failed) only
where tmux is absent, so the suite still collects on a tmux-less CI.

Load-bearing properties:
  * pane_alive(living) -> (True, real int pid)               (mutant: wrong list_targets shape ->
        the REAL detector reads wrong / KeyError -> caught).
  * pane_alive(killed/dead) -> alive False                   (mutant: ignore pane_dead -> caught).
  * jsonl_progress(appended) -> grew True; (flat) -> grew False (mutant: wrong stat read -> caught).

NO IMPLEMENTATION here — harnessd/spawn/tmux.py does not exist yet (RED first: the bind fails).
"""

from __future__ import annotations

import importlib
import os
import shutil
import time
import uuid

import pytest

import harnessd.detector_signals as sig


_HAS_TMUX = shutil.which("tmux") is not None
_real_tmux = pytest.mark.skipif(not _HAS_TMUX, reason="real-tmux test: tmux binary not installed")


def _tmux_mod():
    return importlib.import_module("harnessd.spawn.tmux")


def _rm_socket_file(sock):
    """Remove the dedicated -L socket FILE after kill-server (no socket leak on disk). Best-effort."""
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


@pytest.fixture
def real_tmux_seam():
    """Bind detector_signals._tmux to the REAL tmux module on a DEDICATED socket; restore after.

    This is the mock-contract validation: the Increment-6 detector logic, written against a MOCKED
    tmux, is now run against the REAL boundary. We swap in the real module (set to a unique -L
    socket) for the duration, then restore `_tmux` and kill the dedicated server (no leak).
    """
    mod = _tmux_mod()
    assert hasattr(mod, "set_socket"), (
        "tmux.py must expose a socket seam so this validation drives a DEDICATED server"
    )
    sock = "harness-mc-" + uuid.uuid4().hex[:12]
    prior_socket = getattr(mod, "_SOCKET", None)
    mod.set_socket(sock)

    prior_seam = sig._tmux
    sig._tmux = mod  # the Increment-9 binding the seam was designed for
    # reset the jsonl size cache so a unique node_address starts clean
    sig._size_cache.clear()
    try:
        yield mod, sock
    finally:
        sig._tmux = prior_seam
        subprocess = importlib.import_module("subprocess")
        subprocess.run(["tmux", "-L", sock, "kill-server"], check=False, capture_output=True)
        _rm_socket_file(sock)  # remove the stale socket FILE too (no leak on disk)
        mod.set_socket(prior_socket)
        sig._size_cache.clear()


def _fake_cli(body: str):
    return ["sh", "-c", body]


def _session():
    return "mc-sess-" + uuid.uuid4().hex[:8]


def _node(*, tmux_target, transcript_path=None, node_address=None):
    return {
        "node_address": node_address or ("mc/" + uuid.uuid4().hex[:8] + "#exec"),
        "tmux_target": tmux_target,
        "transcript_path": transcript_path,
    }


def _wait_for_target(mod, session):
    """Return the tmux_target key for a session once it appears in list_targets."""
    for _ in range(50):
        for k in mod.list_targets():
            if k.startswith(session + ":"):
                return k
        time.sleep(0.1)
    return None


# ===========================================================================
# (d.1) pane_alive on a REAL LIVING pane -> (True, real int pid).
# ===========================================================================

@_real_tmux
def test_real_detector_pane_alive_on_living_pane(real_tmux_seam):
    """The REAL detector_signals.pane_alive, bound to REAL tmux, reports a living pane as alive."""
    mod, _sock = real_tmux_seam
    session = _session()
    mod.create_detached(session, _fake_cli("sleep 60"), {"CLAUDE_CONFIG_DIR": "/x"})
    target = _wait_for_target(mod, session)
    assert target is not None, "the living pane must appear in the real list_targets"

    node = _node(tmux_target=target)
    alive, pid = sig.pane_alive(node)
    assert alive is True, (
        "pane_alive on a REAL living pane must be True — this proves real tmux emits the "
        "{pane_dead:0, pane_pid} shape the Increment-6 verdict logic was mocked against"
    )
    assert isinstance(pid, int) and pid > 0, (
        f"pane_alive must return a REAL int pane_pid for a living pane; got {pid!r}"
    )
    mod.kill(session)


# ===========================================================================
# (d.2) pane_alive on a REAL KILLED / DEAD pane -> alive False.
# ===========================================================================

@_real_tmux
def test_real_detector_pane_alive_on_killed_pane(real_tmux_seam):
    """A REAL killed pane (gone from the server) reads as NOT alive through the real seam."""
    mod, _sock = real_tmux_seam
    session = _session()
    mod.create_detached(session, _fake_cli("sleep 60"), {"CLAUDE_CONFIG_DIR": "/x"})
    target = _wait_for_target(mod, session)
    assert target is not None
    node = _node(tmux_target=target)
    assert sig.pane_alive(node)[0] is True  # sanity: alive before kill

    mod.kill(session)
    # after kill the target is gone -> list_targets no longer has it -> pane_alive False
    for _ in range(50):
        alive, _pid = sig.pane_alive(node)
        if not alive:
            break
        time.sleep(0.1)
    alive, pid = sig.pane_alive(node)
    assert alive is False, (
        "pane_alive on a REAL killed pane must be False — the real detector must read the dead/"
        "absent pane correctly (the Increment-6 contract against real tmux)"
    )
    assert pid is None, "a dead/absent pane reports no live pid"


@_real_tmux
def test_real_detector_pane_alive_on_remain_on_exit_dead_pane(real_tmux_seam):
    """A pane whose process EXITED but stays (remain-on-exit) reads pane_dead -> NOT alive.

    Distinguishes 'pane object gone' from 'pane present but dead' — the detector must treat the
    real pane_dead==1 signal as not-alive (mutant: ignore pane_dead -> caught).
    """
    mod, _sock = real_tmux_seam
    session = _session()
    # short-lived process + remain-on-exit so the pane lingers with pane_dead=1.
    mod.create_detached(session, _fake_cli("echo hi; sleep 0.3"), {"CLAUDE_CONFIG_DIR": "/x"})
    target = _wait_for_target(mod, session)
    assert target is not None
    subprocess = importlib.import_module("subprocess")
    subprocess.run(["tmux", "-L", _sock, "set-option", "-t", session, "remain-on-exit", "on"],
                   check=False, capture_output=True)

    node = _node(tmux_target=target)
    # wait for the process to exit; the pane lingers dead. If the impl does NOT keep the pane
    # (no remain-on-exit honored), it becomes absent, which is also not-alive — both acceptable.
    dead_or_gone = False
    for _ in range(50):
        alive, _pid = sig.pane_alive(node)
        if not alive:
            dead_or_gone = True
            break
        time.sleep(0.1)
    assert dead_or_gone, (
        "a pane whose process exited must read as NOT alive (pane_dead or absent) through the "
        "real detector seam"
    )
    mod.kill(session)


# ===========================================================================
# (d.3) jsonl_progress -> grew True for an appended transcript, False for a flat one.
#   The transcript is a REAL file written by the FAKE CLI pane (zero model burn).
# ===========================================================================

@_real_tmux
def test_real_detector_jsonl_progress_grew_then_flat(real_tmux_seam, tmp_path):
    """The REAL jsonl_progress reads a REAL transcript: grew on append, flat when unchanged.

    A FAKE CLI pane appends a JSONL line, then idles. We poll jsonl_progress across the append to
    observe grew=True, then again on the now-static file to observe grew=False. This proves the
    detector's stat-based progress reader works against a real on-disk transcript (the same kind a
    booted agent would write), not just a synthetic fixture.
    """
    mod, _sock = real_tmux_seam
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"line": 0}\n', encoding="utf-8")  # baseline non-empty

    addr = "mc/jsonl-" + uuid.uuid4().hex[:8] + "#exec"
    node = _node(tmux_target="harness:none", transcript_path=str(transcript), node_address=addr)

    # baseline read establishes the cache (grew False — no prior to grow past)
    grew0, mt0 = sig.jsonl_progress(node)
    assert grew0 is False, "first read establishes the baseline (grew False)"
    assert mt0 is not None

    # a FAKE CLI pane appends one line to the REAL transcript file (zero model burn).
    # Build the shell command without str.format (the JSONL braces would collide).
    session = _session()
    append_cmd = "printf '%s\\n' '{\"line\": 1}' >> " + str(transcript) + "; sleep 30"
    mod.create_detached(session, _fake_cli(append_cmd), {"CLAUDE_CONFIG_DIR": "/x"})

    grew_seen = False
    for _ in range(50):
        grew, _mt = sig.jsonl_progress(node)
        if grew:
            grew_seen = True
            break
        time.sleep(0.1)
    assert grew_seen, (
        "jsonl_progress must report grew=True after a REAL append to the transcript — the "
        "forward-progress signal the Increment-6 verdict fuses, validated against a real file"
    )

    # now the file is static -> the next read is flat
    grew_flat, _mt = sig.jsonl_progress(node)
    assert grew_flat is False, (
        "jsonl_progress on an unchanged transcript must report grew=False (no phantom progress)"
    )
    mod.kill(session)
