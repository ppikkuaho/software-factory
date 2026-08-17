"""Increment 6 — detector_signals.* FROZEN acceptance (the raw signal readers).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.8 (the FROZEN detector_signals.py interface).
  - IMPLEMENTATION-PLAN §4.1 "terminal-signal reader (the PRODUCER for INCLUDE-item #3,
    fully mockable)" (L564-571) — the stale-owner_token fence is the load-bearing case.
  - Runtime tree layout (L454-471): nodes/<collapsed-address>/.signal.json carries
    {signal: DONE|FAILED|ESCALATED, ts, owner_token, evidence}, agent-written atomic tmp+rename.

FROZEN INTERFACE (transcribed exactly from §2.8):
    jsonl_progress(node) -> (grew: bool, mtime_iso: str|None)
        # os.stat(transcript) st_size/st_mtime vs a cached prior.
    pane_alive(node) -> (alive: bool, pane_pid: int|None)
        # via the tmux display-message INTERFACE (MOCKED in Inc 6).
    pane_pid_cpu(node, pane_pid) -> float|None
        # WEDGE-PATH ONLY; STUB-return None in v1.
    read_terminal_signal(node, binding) -> dict|None
        # reads /runtime/.../nodes/<addr>/.signal.json. FENCED against binding.owner_token:
        #   returns {signal, ts, owner_token, evidence} IFF file.owner_token == binding.owner_token;
        #   a STALE owner_token -> None (a dead incarnation leftover never collapses a re-spawned node);
        #   absent file -> None.

NO IMPLEMENTATION here — detector_signals.py does not exist yet. RED first.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

import harnessd.fencing as fencing
import harnessd.ledger as ledger


def _signals():
    return importlib.import_module("harnessd.detector_signals")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    return tmp_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_node(tmp_path, node_address, *, transcript_path=None, tmux_target="harness:t"):
    """A minimal node/binding-shaped dict the signal readers operate on."""
    return {
        "node_address": node_address,
        "transcript_path": transcript_path,
        "tmux_target": tmux_target,
    }


# ===========================================================================
# jsonl_progress — real-file size/mtime, grew vs flat (JSONL SYNTHESIZED).
# ===========================================================================

def test_jsonl_progress_grew_then_flat(runtime, tmp_path):
    sig = _signals()
    p = tmp_path / "transcript.jsonl"
    p.write_bytes(b"line-one\n")
    node = _make_node(tmp_path, "proj/a#exec", transcript_path=str(p))

    grew1, mt1 = sig.jsonl_progress(node)
    # First read establishes a baseline; growth after that is the forward-progress signal.
    p.write_bytes(b"line-one\nline-two\n")  # size grew
    # bump mtime forward so st_mtime advances even on coarse-resolution filesystems
    future = datetime.now(timezone.utc).timestamp() + 5
    os.utime(p, (future, future))
    grew2, mt2 = sig.jsonl_progress(node)
    assert grew2 is True, "a transcript that grew in size since the cached prior reads grew=True"
    assert isinstance(mt2, str) and mt2, "mtime_iso must be an ISO string when the file exists"

    # No change -> flat.
    grew3, mt3 = sig.jsonl_progress(node)
    assert grew3 is False, "no size change since the cached prior reads grew=False (flat)"


def test_jsonl_progress_returns_iso_mtime(runtime, tmp_path):
    sig = _signals()
    p = tmp_path / "transcript.jsonl"
    p.write_bytes(b"data\n")
    node = _make_node(tmp_path, "proj/a#exec", transcript_path=str(p))
    _grew, mt = sig.jsonl_progress(node)
    # mtime_iso must parse as an ISO-8601 instant (the detector compares it via the clock).
    assert mt is not None
    datetime.fromisoformat(mt)  # raises if not ISO-8601


# ===========================================================================
# jsonl_progress fail-loud — no transcript_path (spawn<->detector contract violation).
# MUTANT KILL: an impl that returns (False, None) silently on a missing transcript
# would let liveness collapse to idle/dead -> caught at the signal layer too.
# ===========================================================================

def test_jsonl_progress_no_transcript_path_fails_loud(runtime, tmp_path):
    sig = _signals()
    node = _make_node(tmp_path, "proj/a#exec", transcript_path=None)
    try:
        out = sig.jsonl_progress(node)
    except Exception:
        return  # raising is acceptable (fail-loud)
    # The only acceptable non-raising outcome is an EXPLICIT unknown sentinel —
    # never a silent (False, None) that reads as benign 'flat'.
    grew, mt = out
    assert not (grew is False and mt is None), (
        "missing transcript_path must fail loud (raise) — not silently return (False, None)"
    )


# ===========================================================================
# pane_pid_cpu — one process-table snapshot, proper descendants only.
# ===========================================================================

def test_pane_pid_cpu_sums_only_proper_descendants(runtime, tmp_path, monkeypatch):
    sig = _signals()
    node = _make_node(tmp_path, "proj/a#exec", transcript_path=str(tmp_path / "t.jsonl"))

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "100 1 0.0\n"
                "101 100 0.0\n"
                "102 101 2.5\n"
                "200 1 91.0\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(sig.subprocess, "run", fake_run)

    assert sig.pane_pid_cpu(node, 100) == 2.5


def test_pane_pid_cpu_zero_and_unknown_are_distinct(runtime, tmp_path, monkeypatch):
    sig = _signals()
    node = _make_node(tmp_path, "proj/a#exec", transcript_path=str(tmp_path / "t.jsonl"))

    monkeypatch.setattr(
        sig.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="100 1 0.0\n101 100 0.0\n",
            stderr="",
        ),
    )
    assert sig.pane_pid_cpu(node, 100) == 0.0
    assert sig.pane_pid_cpu(node, 999) is None

    monkeypatch.setattr(
        sig.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="ps failed",
        ),
    )
    assert sig.pane_pid_cpu(node, 100) is None


# ===========================================================================
# read_terminal_signal — the FENCED .signal.json reader.
# nodes/<collapsed-address>/.signal.json carrying {signal, ts, owner_token, evidence}.
# ===========================================================================

import harnessd.addressing as _addressing


def _node_dir(runtime, node_address):
    """The node dir per the canonical NESTED layout (addressing.node_dir)."""
    d = _addressing.node_dir(node_address, runtime)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_signal(runtime, node_address, *, signal, owner_token, evidence=None):
    # Write through the SAME canonical derivation the reader uses (nested dir + per-seat signal file),
    # so the test writer and detector_signals.read_terminal_signal can never drift on the layout.
    path = _addressing.signal_path(node_address, runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signal": signal,
        "ts": _now(),
        "owner_token": owner_token,
        "evidence": evidence or {},
    }
    path.write_text(json.dumps(payload))
    return payload


def _binding(node_address, *, owner_token):
    return {"node_address": node_address, "owner_token": owner_token}


def test_read_terminal_signal_live_token_returns_signal(runtime, tmp_path):
    """A DONE .signal.json whose owner_token == binding.owner_token is returned."""
    sig = _signals()
    addr = "proj/a#exec"
    token = fencing.mint_owner_token(addr, "sa", "uuid", 3)
    _write_signal(runtime, addr, signal="DONE", owner_token=token,
                  evidence={"report": "report.md"})
    node = _make_node(tmp_path, addr, transcript_path=str(tmp_path / "t.jsonl"))
    out = sig.read_terminal_signal(node, _binding(addr, owner_token=token))
    assert out is not None, "a live-token DONE signal must be returned (the collapse producer)"
    assert out["signal"] == "DONE"
    assert out["owner_token"] == token


# ---- THE LOAD-BEARING FENCE: a STALE owner_token -> None ----
def test_read_terminal_signal_stale_owner_token_returns_none(runtime, tmp_path):
    """A DONE signal with a PRIOR-epoch owner_token must return None.

    MUTANT KILL (ignore the fence): if the reader returns the signal regardless of
    owner_token, a dead incarnation's leftover .signal.json would collapse a
    RE-SPAWNED node at the same address. The fence is the only thing that prevents
    that — this test fails iff the fence is dropped.
    """
    sig = _signals()
    addr = "proj/a#exec"
    # leftover from a PRIOR incarnation (epoch 2) ...
    stale_token = fencing.mint_owner_token(addr, "sa-old", "uuid-old", 2)
    _write_signal(runtime, addr, signal="DONE", owner_token=stale_token)
    # ... while the LIVE binding has been re-spawned at a higher epoch (epoch 3).
    live_token = fencing.mint_owner_token(addr, "sa-new", "uuid-new", 3)
    node = _make_node(tmp_path, addr, transcript_path=str(tmp_path / "t.jsonl"))

    out = sig.read_terminal_signal(node, _binding(addr, owner_token=live_token))
    assert out is None, (
        "a STALE owner_token must return None — a dead incarnation's leftover signal "
        "must NEVER collapse a re-spawned node at the same address"
    )


def test_read_terminal_signal_absent_returns_none(runtime, tmp_path):
    """No .signal.json present -> None (not an error, not a phantom signal)."""
    sig = _signals()
    addr = "proj/a#exec"
    _node_dir(runtime, addr)  # dir exists, but no .signal.json
    live_token = fencing.mint_owner_token(addr, "sa", "uuid", 1)
    node = _make_node(tmp_path, addr, transcript_path=str(tmp_path / "t.jsonl"))
    assert sig.read_terminal_signal(node, _binding(addr, owner_token=live_token)) is None


def test_read_terminal_signal_escalated_live_token_returned(runtime, tmp_path):
    """An ESCALATED signal with a live token is returned (the watchdog routes it to NOOP).

    The reader does NOT itself decide collapse-vs-noop; it returns the fenced signal.
    This guards that read_terminal_signal does not filter ESCALATED out (which would
    erase the waiting-reason the verdict needs).
    """
    sig = _signals()
    addr = "proj/a#exec"
    token = fencing.mint_owner_token(addr, "sa", "uuid", 1)
    _write_signal(runtime, addr, signal="ESCALATED", owner_token=token)
    node = _make_node(tmp_path, addr, transcript_path=str(tmp_path / "t.jsonl"))
    out = sig.read_terminal_signal(node, _binding(addr, owner_token=token))
    assert out is not None and out["signal"] == "ESCALATED"
