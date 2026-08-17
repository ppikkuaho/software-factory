"""Increment 6 — load-bearing STRENGTHENING (mutation-review gate + bias-to-real).

Gaps the reviews flagged (two SURVIVING mutants + an inert spec dimension + an off-by-one):

  1. (surviving mutant) pane_alive's OWN pane_dead-vs-pane-gone branch was untested (no Inc-6 test
     exercises it — _tmux is None). Inject a FAKE _tmux exposing the real §2.11 list_targets shape and
     test pane_alive's parsing for real: pane_dead=1 -> (False,None); pane_dead=0 -> (True,pid); gone -> (False,None).
     (The Inc-9 real-tmux contract test then validates this shape against REAL tmux — Lesson 6.)
  2. (surviving mutant) a stamp-less binding (last_progress_at=None), flat + warm + no reason, must read
     'idle' (actionable-flat), NEVER 'working' — else a never-progressed node is masked as working forever.
  3. (MEDIUM, fixed) _w_window now keys off suspicion_window_key (task-type), reachable, not the inert
     liveness_state. Pin that a non-'working' key selects the longer window.
  4. (off-by-one, fixed) the W boundary is spec-exact: at age == W the node is still 'working' (overdue iff age > W).
"""

import json

import harnessd.detector as detector
import harnessd.detector_signals as ds
import harnessd.ledger as ledger


# --- signal-layer: pane_alive parses the real §2.11 list_targets shape (fake _tmux) ----------------

class _FakeTmux:
    def __init__(self, targets):
        self._targets = targets

    def list_targets(self):
        return self._targets


def test_pane_alive_pane_dead_flag(monkeypatch):
    """pane_dead==1 -> (False, None): a dead-but-present pane is NOT alive (distinct from pane-gone)."""
    monkeypatch.setattr(ds, "_tmux", _FakeTmux({"w:0.0": {"pane_pid": 123, "pane_dead": 1}}))
    assert ds.pane_alive({"node_address": "a", "tmux_target": "w:0.0"}) == (False, None)


def test_pane_alive_warm_pane(monkeypatch):
    """pane_dead==0 -> (True, pid)."""
    monkeypatch.setattr(ds, "_tmux", _FakeTmux({"w:0.0": {"pane_pid": 123, "pane_dead": 0}}))
    assert ds.pane_alive({"node_address": "a", "tmux_target": "w:0.0"}) == (True, 123)


def test_pane_alive_pane_gone(monkeypatch):
    """A target absent from list_targets -> (False, None) (pane gone entirely)."""
    monkeypatch.setattr(ds, "_tmux", _FakeTmux({}))
    assert ds.pane_alive({"node_address": "a", "tmux_target": "w:0.0"}) == (False, None)


# --- verdict-layer: stamp-less binding must read idle, not working --------------------------------

def _seed(tmp_path, monkeypatch, **fields):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    addr = "proj/a#exec"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    rec = {"node_address": addr, "state": "running", "generation": 0,
           "owner_token": "proj/a#exec:sa:uuid:1", "lease_epoch": 1,
           "transcript_path": str(transcript), "tmux_target": "w:0.0"}
    rec.update(fields)
    ledger.write_binding({addr: rec}, _lock_held=True)
    return addr, transcript


def test_stampless_binding_reads_idle_not_working(tmp_path, monkeypatch):
    """last_progress_at=None + flat + warm + no reason -> idle (a never-stamped node is actionable-flat,
    NEVER masked as working forever)."""
    addr, transcript = _seed(tmp_path, monkeypatch, last_progress_at=None, terminal_signal=None)
    # prime jsonl_progress baseline (first read = flat), then read flat again
    monkeypatch.setattr(ds, "pane_alive", lambda node: (True, 123))  # warm pane
    detector.liveness(addr)            # baseline read
    assert detector.liveness(addr).state == "idle"


# --- _w_window: the task-type key is reachable (no longer inert) ----------------------------------

def test_corrupt_signal_json_is_contained_fail_loud_not_a_crash_and_not_silent(tmp_path, monkeypatch):
    """RR-2 (supersedes the propagate-pin): a corrupt .signal.json is AGENT-WRITTEN input — it must
    neither crash the daemon (the old JSONDecodeError propagated through reconcile_tick -> poll_loop
    -> process death, a deterministic relaunch crash-loop: the poison file survives relaunch) NOR
    silently degrade the node. Contained fail-loud: the verdict still computes (here `waiting` — the
    binding's own ESCALATED stamp carries the reason), the fault is journaled
    ``signal_artifact_invalid``, and the artifact is quarantined to ``*.invalid``."""
    addr, transcript = _seed(tmp_path, monkeypatch, last_progress_at="2020-01-01T00:00:00+00:00",
                             terminal_signal="ESCALATED")  # flat beyond W, escalated
    monkeypatch.setattr(ds, "pane_alive", lambda node: (True, 123))  # warm pane
    # write a CORRUPT signal at the node's canonical per-seat signal path (nested derivation)
    import harnessd.addressing as _addressing
    sigp = _addressing.signal_path(addr, tmp_path)
    sigp.parent.mkdir(parents=True, exist_ok=True)
    sigp.write_text("{not valid json", encoding="utf-8")
    # flat-beyond-W (grew=False on a baseline read) reaches step 6 -> reads the corrupt signal ->
    # the reader REJECTS it (WATCHDOG SS7: torn artifacts are rejected, not adopted) and the verdict
    # falls back to the binding's ESCALATED stamp: `waiting`, never a raise, never a silent idle.
    verdict = detector.liveness(addr)
    assert verdict.state == "waiting", (
        f"the escalated node must still read 'waiting' off its binding stamp (got {verdict.state!r})"
    )
    # NOT SILENT: the rejection is journaled and the artifact quarantined for inspection.
    events = [r.get("event") for r in ledger.load_wal() if r.get("node_address") == addr]
    assert "signal_artifact_invalid" in events, "the malformed artifact must be journaled (RR-2)"
    assert not sigp.exists() and sigp.with_name(sigp.name + ".invalid").exists(), (
        "the malformed artifact must be quarantined to *.invalid"
    )


def test_w_window_task_type_key_reachable():
    """suspicion_window_key='waiting_on_child' selects the LONGER 600s window, not the 120s floor."""
    assert detector._w_window({"suspicion_window_key": "waiting_on_child"}) == 600
    assert detector._w_window({"suspicion_window_key": "working"}) == 120
    assert detector._w_window({}) == 120  # absent -> safe tight floor


# --- W boundary is spec-exact: at age == W still working ------------------------------------------

def test_within_w_boundary_is_inclusive(monkeypatch):
    """At EXACTLY age == W the node is still within-W (working); overdue only when age > W.

    Deterministic (the == boundary is measure-zero in wall-clock): control the age directly.
    """
    import harnessd.clock as clock
    w = 120
    monkeypatch.setattr(clock, "age_seconds", lambda ts: float(w))       # age == W
    assert detector._within_w("2026-01-01T00:00:00+00:00", w) is True, "age == W -> within-W (inclusive)"
    monkeypatch.setattr(clock, "age_seconds", lambda ts: float(w) + 0.001)  # age just over W
    assert detector._within_w("2026-01-01T00:00:00+00:00", w) is False, "age > W -> overdue (beyond W)"
