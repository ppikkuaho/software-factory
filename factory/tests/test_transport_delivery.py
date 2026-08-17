"""The live delivery transport — the pane becomes reachable, watchable, and driven (CRIT-2 / ③-wake).

The last code increment before the first live run. What this pins:

  * tmux.send_keys(target, text) — literal text then Enter (two send-keys calls, -l for the
    literal: the interactive_eval precedent — CC's input box submits on Enter).
  * tmux.create_detached(..., cwd=...) — the pane boots in the node's workspace (-c), so the
    kickoff pointer's relative reads (brief.md, .inbox.<seat>.jsonl) agree with the pane cwd.
  * watchdog._capture_pane — the REAL capture-pane read behind the SAME module seam the existing
    watchdog tests monkeypatch (seam name/shape preserved; those tests stay green unmodified).
  * FORK_PROMPT — the golden idle-prompt string, measured on the pinned CC v2.1.152 and pinned
    by a captured fixture (tests/fixtures/cc-2.1.152-idle-pane.txt).
  * the daemon ENACTS keystroke actions: a PROD's detail['keystroke'] is actually delivered via
    tmux.send_keys(binding tmux_target); the ③-wake (unacked inbox -> pointer nudge) is wired
    with its edge-trigger watermark advanced through the single-writer executor.
  * the chokepoint's kickoff: durable-artifact-FIRST (a kickoff line appended to the node's
    .inbox.<seat>.jsonl) then a best-effort send-keys pointer — a lost keystroke is healed by
    the watchdog wake on the unacked inbox line.
  * argv: --system-prompt-file is ABSOLUTE (survives any cwd); --model is derived from
    level_config.model via the config mapping (probed live: CC v2.1.152 serves
    'claude-opus-5' through a real turn).
  * deterministic trust is seeded for the ACTUAL pane cwd on EVERY spawn (unjailed included) —
    an untrusted cwd would freeze the agent on the trust dialog.

Real-tmux tests follow the repo pattern: per-test dedicated `-L` socket + kill-server teardown.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.daemon as daemon
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.watchdog as watchdog
from harnessd.spawn import chokepoint
from harnessd.spawn.adapters.claude_code import ClaudeCodeAdapter


_HAS_TMUX = shutil.which("tmux") is not None
_real_tmux = pytest.mark.skipif(not _HAS_TMUX, reason="real-tmux test: tmux binary not installed")

_FIXTURE = Path(__file__).parent / "fixtures" / "cc-2.1.152-idle-pane.txt"


def _tmux_mod():
    return importlib.import_module("harnessd.spawn.tmux")


def _rm_socket_file(sock):
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
def tmux_server():
    sock = "tport-" + uuid.uuid4().hex[:12]
    mod = _tmux_mod()
    prior = getattr(mod, "_SOCKET", None)
    mod.set_socket(sock)
    try:
        yield mod, sock
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], check=False, capture_output=True)
        _rm_socket_file(sock)
        mod.set_socket(prior)


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


# ===========================================================================
# (1) tmux.send_keys — literal text then Enter (the interactive_eval precedent).
# ===========================================================================

@_real_tmux
def test_send_keys_delivers_literal_text_to_a_real_pane(tmux_server):
    """send_keys(target, text) types the LITERAL text (-l) then Enter into the live pane —
    proven by a `cat` pane echoing the line back into the capture buffer."""
    mod, _sock = tmux_server
    target = mod.create_detached("tport-cat", ["sh", "-c", "cat"], {"CLAUDE_CONFIG_DIR": "/x"})

    mod.send_keys(target, "hello transport -l literal $NOT_EXPANDED")

    captured = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        captured = mod.capture_pane(target)
        if "hello transport" in captured:
            break
        time.sleep(0.1)
    assert "hello transport -l literal $NOT_EXPANDED" in captured, (
        f"send_keys must deliver the literal text (no shell expansion, -l); pane: {captured!r}"
    )
    mod.kill(target)


# ===========================================================================
# (2) tmux.create_detached cwd — the pane boots in the node's workspace (-c).
# ===========================================================================

@_real_tmux
def test_create_detached_cwd_boots_the_pane_in_the_given_dir(tmux_server, tmp_path):
    mod, _sock = tmux_server
    workdir = tmp_path / "node-ws"
    workdir.mkdir()
    target = mod.create_detached(
        "tport-cwd", ["sh", "-c", "pwd; echo CWD_END; sleep 30"],
        {"CLAUDE_CONFIG_DIR": "/x"}, cwd=str(workdir),
    )
    captured = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        captured = mod.capture_pane(target)
        if "CWD_END" in captured:
            break
        time.sleep(0.1)
    real = os.path.realpath(str(workdir))
    # tmux capture-pane wraps long output at pane width; remove physical newlines for this cwd
    # substring check so a path split as ".../no\nde-ws" still proves the -c cwd was honored.
    unwrapped = captured.replace("\n", "")
    assert real in captured or str(workdir) in captured or real in unwrapped or str(workdir) in unwrapped, (
        f"the pane must boot with cwd={workdir} (-c); pane printed: {captured!r}"
    )
    mod.kill(target)


# ===========================================================================
# (3) watchdog._capture_pane — the REAL read behind the SAME seam.
# ===========================================================================

@_real_tmux
def test_watchdog_capture_pane_reads_the_real_pane(tmux_server):
    mod, _sock = tmux_server
    target = mod.create_detached(
        "tport-cap", ["sh", "-c", "echo PANE_MARKER_42; sleep 30"], {"CLAUDE_CONFIG_DIR": "/x"}
    )
    node = {"node_address": "p/x#exec", "tmux_target": target, "transcript_path": None}
    captured = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        captured = watchdog._capture_pane(node)
        if "PANE_MARKER_42" in captured:
            break
        time.sleep(0.1)
    assert "PANE_MARKER_42" in captured, (
        "watchdog._capture_pane must read the REAL pane buffer through tmux.capture_pane "
        f"(the prod-gate evidence); got {captured!r}"
    )
    mod.kill(target)


def test_watchdog_capture_pane_without_target_reads_empty_gate_closed():
    """A node with no tmux_target reads an EMPTY pane (gate closed — conservative), no crash."""
    assert watchdog._capture_pane({"node_address": "p/x#exec"}) == ""
    assert watchdog.prod_precondition({"node_address": "p/x#exec"}) is False


def test_watchdog_seam_is_still_monkeypatchable(monkeypatch):
    """The existing tests' seam contract holds: monkeypatching watchdog._capture_pane changes
    what prod_precondition reads (the seam name/shape is preserved by the real wiring)."""
    monkeypatch.setattr(watchdog, "_capture_pane", lambda _node: f"x\n{watchdog.FORK_PROMPT} \ny")
    assert watchdog.prod_precondition({"node_address": "p/x#exec"}) is True
    monkeypatch.setattr(watchdog, "_capture_pane", lambda _node: "no prompt here")
    assert watchdog.prod_precondition({"node_address": "p/x#exec"}) is False


# ===========================================================================
# (4) FORK_PROMPT — the golden idle-prompt string, pinned by the CAPTURED CC fixture.
# ===========================================================================

def test_fork_prompt_matches_the_captured_cc_idle_pane(monkeypatch):
    """The golden string is measured, not guessed: the REAL CC v2.1.152 idle pane (captured live
    2026-06-10, model claude-opus-4-8, zero dialogs) must open the prod gate. FORK-PROMPT says the
    string is pinned per CC version — this fixture IS that pin."""
    assert _FIXTURE.is_file(), f"captured fixture missing: {_FIXTURE}"
    pane = _FIXTURE.read_text(encoding="utf-8")
    assert watchdog.FORK_PROMPT != "FORK_PROMPT", (
        "FORK_PROMPT is still the placeholder — it must be the measured CC v2.1.152 idle marker"
    )
    assert watchdog.FORK_PROMPT in pane, (
        f"the pinned golden string {watchdog.FORK_PROMPT!r} must appear in the captured idle pane"
    )
    monkeypatch.setattr(watchdog, "_capture_pane", lambda _node: pane)
    assert watchdog.prod_precondition({"node_address": "p/x#exec"}) is True, (
        "the REAL captured idle pane must open the prod gate"
    )


def test_prod_gate_refuses_a_working_pane(monkeypatch):
    """A pane mid-generation (the 'esc to interrupt' working marker) must NOT be prodded even
    though CC renders the ❯ input box while streaming — Precondition 1: never corrupt an
    in-flight turn's input line."""
    working = _FIXTURE.read_text(encoding="utf-8").replace("? for shortcuts", "esc to interrupt")
    monkeypatch.setattr(watchdog, "_capture_pane", lambda _node: working)
    assert watchdog.prod_precondition({"node_address": "p/x#exec"}) is False, (
        "a working pane (esc to interrupt) must keep the prod gate CLOSED"
    )


@pytest.mark.parametrize("fixture_name", [
    "cc-2.1.152-trust-dialog-pane.txt",     # trust dialog ('Enter to confirm · Esc to cancel')
    "cc-2.1.152-tool-approval-pane.txt",    # tool-approval dialog ('Esc to cancel · Tab to amend')
])
def test_prod_gate_refuses_a_blocking_dialog_pane(monkeypatch, fixture_name):
    """A blocking DIALOG pane (captured live: the CC trust dialog AND the tool-approval dialog)
    must keep the gate CLOSED — both render '❯' as the SELECTION cursor, and a nudge's Enter
    would press the highlighted option ('Yes, I trust this folder' / 'Yes'). Deterministic trust
    + the jail's skip-permissions make dialogs structurally absent; this is the belt-and-braces
    refusal, pinned by the real captured dialogs."""
    dialog_fixture = Path(__file__).parent / "fixtures" / fixture_name
    assert dialog_fixture.is_file()
    pane = dialog_fixture.read_text(encoding="utf-8")
    assert watchdog.FORK_PROMPT in pane, "the dialog DOES render the prompt char (why the guard exists)"
    monkeypatch.setattr(watchdog, "_capture_pane", lambda _node: pane)
    assert watchdog.prod_precondition({"node_address": "p/x#exec"}) is False, (
        "a blocking dialog pane must keep the prod gate CLOSED (Enter would confirm the selection)"
    )


# ===========================================================================
# (5) The daemon ENACTS the watchdog's keystroke actions (PROD + the ③-wake).
# ===========================================================================

LEAF = "proj/widget/task#exec"


class _RecordingTmuxTransport:
    """A list_targets+send_keys fake for the daemon tick (the binding's pane is 'alive')."""

    def __init__(self, targets):
        self._targets = dict(targets)
        self.sent = []  # (target, text)

    def list_targets(self):
        return dict(self._targets)

    def send_keys(self, target, text):
        self.sent.append((target, text))

    def kill(self, target):
        pass


class _Detector:
    def __init__(self, state, last_progress_at=None):
        self._state = state
        self._lp = last_progress_at

    def liveness(self, node_address):
        from types import SimpleNamespace
        return SimpleNamespace(state=self._state, last_progress_at=self._lp)


def _seed_leaf(runtime, **overrides):
    token = fencing.mint_owner_token(LEAF, "sa", "uuid", 1)
    ws = addressing.node_dir(LEAF, runtime)
    ws.mkdir(parents=True, exist_ok=True)
    target = addressing.session_name_for(LEAF) + ":0.0"
    rec = {
        "node_address": LEAF, "parent_address": "proj/widget#exec", "level": "L5",
        "subagent_id": "sa", "session_uuid": "uuid", "state": "running", "generation": 1,
        "lease_epoch": 1, "owner_token": token, "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md",
        "liveness_state": "working", "terminal_signal": None, "gate_crossed_at": None,
        "paused_at": None, "transcript_path": str(ws / "t.jsonl"),
        "tmux_target": target, "workspace": str(ws),
        "stale_check_count": 0, "stale_grace_checks": 2,
        "last_inbox_acked_offset": 0,
    }
    rec.update(overrides)
    ledger.write_binding({LEAF: copy.deepcopy(rec)}, _lock_held=True)
    return rec, token, target


def _ago_iso(seconds):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_daemon_delivers_the_prod_keystroke_via_send_keys(runtime, monkeypatch):
    """A PROD action's detail['keystroke'] is ACTUALLY sent to the binding's tmux_target —
    the keystone the review found missing (an un-enacted PROD nudges nobody)."""
    rec, _token, target = _seed_leaf(runtime, last_progress_at=_ago_iso(9999))
    tmux = _RecordingTmuxTransport({target: {"pane_pid": 4242, "pane_dead": 0}})
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("idle", _ago_iso(9999)))

    assert tmux.sent, "the daemon must DELIVER the PROD keystroke via tmux.send_keys"
    sent_target, sent_text = tmux.sent[0]
    assert sent_target == target, f"the keystroke must target the binding's tmux_target; got {sent_target!r}"
    assert "produce what you owe" in sent_text.lower()
    assert "explain" in sent_text.lower() and "escalate" in sent_text.lower()
    assert "new message" not in sent_text.lower(), (
        "the leaf nonresponse prod must not impersonate an inbox notification with no unread row"
    )


def test_daemon_wakes_a_node_with_an_unacked_inbox_line_and_advances_the_watermark(runtime, monkeypatch):
    """The ③-wake, wired end-to-end: an unacked .inbox.<seat>.jsonl line nudges the pane ONCE
    (a pointer, never the payload) and the edge-trigger watermark (last_inbox_acked_offset)
    advances through the single-writer executor once the nudge is VERIFIED consumed (R-1:
    the transcript grew — never on send-keys rc=0 alone); a tick with nothing new sends nothing."""
    rec, _token, target = _seed_leaf(runtime)
    inbox = addressing.inbox_path(LEAF, runtime)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"from": "parent", "type": "answer_posted", "message": "SECRET-PAYLOAD"}) + "\n")
    size = inbox.stat().st_size

    tmux = _RecordingTmuxTransport({target: {"pane_pid": 4242, "pane_dead": 0}})
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))  # healthy -> no PROD; wake fires

    assert len(tmux.sent) == 1, f"exactly ONE wake nudge for the new line; got {tmux.sent!r}"
    sent_target, sent_text = tmux.sent[0]
    assert sent_target == target
    assert "inbox" in sent_text.lower(), "the wake keystroke is the POINTER at the inbox re-read"
    assert "SECRET-PAYLOAD" not in sent_text, "the wake is a pointer, NEVER the message payload"

    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "R-1: rc=0 is NOT consumption — the watermark must not advance until the transcript grows"
    )
    assert live.get("wake_pending_ack_offset") == size, (
        "the delivered nudge records its pending verification at the pre-send inbox size; got "
        f"{live.get('wake_pending_ack_offset')!r}"
    )

    # The agent consumes the nudge: a new turn lands in the transcript JSONL.
    with open(rec["transcript_path"], "w", encoding="utf-8") as fh:
        fh.write('{"type": "assistant", "turn": "new"}\n')

    daemon._watchdog_tick(executor, tmux, _Detector("working"))  # verified -> ack, no new nudge
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset") == size, (
        "the verified wake must advance the watermark to the pre-send size (edge-triggered, "
        f"one nudge per new line); got {live.get('last_inbox_acked_offset')!r}"
    )
    assert len(tmux.sent) == 1, "the resolving tick owes no new nudge (nothing else unacked)"

    daemon._watchdog_tick(executor, tmux, _Detector("working"))  # nothing new -> no second nudge
    assert len(tmux.sent) == 1, "a re-tick with NOTHING new must not re-nudge (no per-poll storm)"


def test_daemon_wake_does_not_ack_textual_tool_invocation_as_consumed(runtime, monkeypatch):
    """A malformed Claude textual tool invocation grows the transcript but did not consume the wake.

    R42 showed this shape: the review seat received a wake to re-read its inbox,
    then emitted ``court`` plus an XML-ish ``<invoke name="Bash">`` block as
    plain assistant text and ended the turn. That must not advance
    ``last_inbox_acked_offset``; the still-unacked inbox row should trigger the
    normal wake retry instead of being lost.
    """
    rec, _token, target = _seed_leaf(runtime)
    inbox = addressing.inbox_path(LEAF, runtime)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"from": "harnessd", "type": "child_collapsed"}) + "\n")
    size = inbox.stat().st_size

    tmux = _RecordingTmuxTransport({target: {"pane_pid": 4242, "pane_dead": 0}})
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))
    assert len(tmux.sent) == 1

    Path(rec["transcript_path"]).write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-06-20T00:19:53.121Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "court\n<invoke name=\"Bash\">\n"
                                "<parameter name=\"command\">tail -n +3 .inbox.review.jsonl</parameter>\n"
                                "<parameter name=\"description\">Read new inbox rows</parameter>\n"
                                "</invoke>"
                            ),
                        }
                    ],
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "textual pseudo-tool output must not prove the inbox wake was consumed"
    )
    assert live.get("wake_pending_ack_offset") == size
    assert len(tmux.sent) == 2, "the unacked inbox row should be retried through the normal wake path"


def test_daemon_wake_respects_the_prod_gate_and_retries_later(runtime, monkeypatch):
    """A closed prod gate (pane mid-turn) suppresses the wake AND leaves the watermark unmoved,
    so the next tick retries — the nudge is deferred, never lost."""
    rec, _token, target = _seed_leaf(runtime)
    inbox = addressing.inbox_path(LEAF, runtime)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"from": "parent", "msg": "wake"}) + "\n")

    tmux = _RecordingTmuxTransport({target: {"pane_pid": 4242, "pane_dead": 0}})
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: False)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    assert not tmux.sent, "a gate-closed pane must NOT be woken (never corrupt an in-flight turn)"
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset", 0) == 0, (
        "the watermark must NOT advance on a suppressed wake — the next tick retries"
    )


def test_daemon_wake_skips_a_paused_subtree(runtime, monkeypatch):
    """WATCHDOG §3.4 STEP 0 extends to the wake: a paused subtree gets no recovery nudge."""
    rec, _token, target = _seed_leaf(runtime, paused_at="2026-06-10T00:00:00+00:00")
    inbox = addressing.inbox_path(LEAF, runtime)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"from": "parent", "msg": "wake"}) + "\n")
    tmux = _RecordingTmuxTransport({target: {"pane_pid": 4242, "pane_dead": 0}})
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    assert not tmux.sent, "a PAUSED subtree must receive no wake nudge (no recovery action, §3.4)"


def test_apply_global_seams_binds_the_detector_tmux_seam(runtime):
    """Production wiring: _apply_global_seams binds detector_signals._tmux to the real wrapper —
    without it pane_alive RAISES on every real tick (the seam was only ever bound in tests)."""
    from types import SimpleNamespace
    prior = detector_signals._tmux
    try:
        daemon._apply_global_seams(SimpleNamespace(runtime_root=runtime, tmux_socket=None))
        assert detector_signals._tmux is _tmux_mod(), (
            "_apply_global_seams must bind detector_signals._tmux = harnessd.spawn.tmux "
            "(pane_alive's §2.11 seam) — production liveness reads through it"
        )
    finally:
        detector_signals._tmux = prior


# ===========================================================================
# (6) The kickoff — durable-artifact-FIRST (inbox line) + best-effort pointer nudge.
# ===========================================================================

class _KickoffTmux:
    """The integration-B RecordingTmux shape + send_keys, mirroring the post-F18 contract."""

    def __init__(self):
        self.created = []
        self.sent = []

    def build_pane_argv(self, env, argv):
        pane = ["env", "-i"]
        for k, v in env.items():
            pane.append(f"{k}={v}")
        return pane + list(argv)

    def create_detached(self, session_name, pane_argv, env, cwd=None):
        self.created.append((session_name, list(pane_argv), dict(env), cwd))
        return f"{session_name}:0.0"

    def send_keys(self, target, text):
        self.sent.append((target, text))
        return True

    def server_env(self):
        return {}

    def capture_pane(self, target):
        # The freshly-booted pane AT the idle input prompt (the measured CC v2.1.152 markers) —
        # the kickoff prompt-gate (LT-6) polls this through the adapter's OWN seam before typing.
        return f"{watchdog.FORK_PROMPT} \n? for shortcuts"

    def list_targets(self):
        return {}

    def kill(self, target):
        pass


def _drive_real_spawn(runtime):
    """Drive ONE spawn through the REAL chokepoint + REAL ClaudeCodeAdapter (recording tmux)."""
    tmux = _KickoffTmux()
    adapter = ClaudeCodeAdapter(tmux=tmux)
    prior = chokepoint.ADAPTER
    chokepoint.set_adapter(adapter)

    node = LEAF
    token = fencing.mint_owner_token(node, "sa", "uuid", 1)
    ws = addressing.node_dir(node, runtime)
    ws.mkdir(parents=True, exist_ok=True)
    rec = {
        "node_address": node, "parent_address": "proj/widget#exec", "level": "L5",
        "subagent_id": "sa", "session_uuid": "uuid", "state": "planned", "generation": 0,
        "lease_epoch": 1, "owner_token": token, "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md",
        "liveness_state": "claimed", "terminal_signal": None, "gate_crossed_at": None,
        "paused_at": None, "transcript_path": None,
        "tmux_target": addressing.session_name_for(node), "workspace": str(ws),
    }
    ledger.write_binding({node: copy.deepcopy(rec)}, _lock_held=True)
    try:
        result = chokepoint.claim_and_spawn(
            node, expected_state="planned", expected_generation=0,
            expected_owner_token=token, level_config=config.LevelConfig.for_level("L3"),
        )
    finally:
        chokepoint.set_adapter(prior)
    return result, tmux, ws


def test_kickoff_appends_a_durable_inbox_line_then_nudges_the_pointer(runtime):
    """After STEP5 the chokepoint delivers the starting instruction: the durable artifact FIRST
    (a kickoff line in .inbox.<seat>.jsonl), then a best-effort send-keys POINTER. A lost
    keystroke is healed by the watchdog ③-wake on the unacked inbox line."""
    result, tmux, ws = _drive_real_spawn(runtime)
    assert result.ok, f"spawn must succeed: {result!r}"

    # (a) the DURABLE artifact: a kickoff line in the node's own inbox.
    inbox = addressing.inbox_path(LEAF, runtime)
    assert inbox.is_file(), "the kickoff must append a line to the node's .inbox.<seat>.jsonl"
    lines = [json.loads(l) for l in inbox.read_text(encoding="utf-8").splitlines() if l.strip()]
    kick = [l for l in lines if l.get("type") == "kickoff"]
    assert kick, f"a kickoff-typed inbox line must land; got {lines!r}"
    assert str(ws) in kick[0].get("message", ""), "the kickoff names the node workspace (the pointer)"

    # (b) the best-effort NUDGE: the pointer typed into the live pane (the canonical target).
    assert tmux.sent, "the kickoff pointer must be send-keys'd into the pane (best-effort nudge)"
    sent_target, sent_text = tmux.sent[0]
    assert sent_target == result.tmux_target, "the nudge targets the canonical recorded tmux_target"
    assert "1 new message: 1 from harnessd" in sent_text
    assert LEAF not in sent_text and "brief.md" not in sent_text and str(ws) not in sent_text, (
        "the notification carries routing metadata plus the inbox pointer; startup content stays "
        f"in the durable kickoff row; got {sent_text!r}"
    )
    assert ".inbox.exec.jsonl" in sent_text, "the pointer names the inbox the messages arrive in"


def test_kickoff_is_a_pointer_never_the_brief_payload(runtime):
    """The wake_keystroke discipline: the pane nudge POINTS at brief.md — it never inlines the
    brief/task content (pointer-not-payload)."""
    (addressing.node_dir(LEAF, runtime)).mkdir(parents=True, exist_ok=True)
    brief_path = addressing.node_dir(LEAF, runtime) / "brief.md"
    brief_path.write_text("# brief\nSECRET-TASK-CONTENT-XYZ\n", encoding="utf-8")
    result, tmux, _ws = _drive_real_spawn(runtime)
    assert result.ok
    for _target, text in tmux.sent:
        assert "SECRET-TASK-CONTENT-XYZ" not in text, "the kickoff must be a pointer, never the payload"


def test_spawn_passes_the_node_workspace_as_the_pane_cwd(runtime):
    """The adapter hands the node's workspace dir to create_detached as cwd — the agent boots
    where its brief lands, so the kickoff's relative reads agree."""
    result, tmux, ws = _drive_real_spawn(runtime)
    assert result.ok
    assert tmux.created, "create_detached must have been called"
    cwd = tmux.created[0][3]
    assert cwd == str(ws), f"the pane cwd must be the node workspace {ws}; got {cwd!r}"


# ===========================================================================
# (7) argv — absolute --system-prompt-file + --model from the config mapping.
# ===========================================================================

def _iso_env(config_dir="/HARNESS/.cc-pinned/config"):
    return {
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-xyz",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def _adapter_argv(model="opus-5.0", env=None):
    tmux = _KickoffTmux()
    adapter = ClaudeCodeAdapter(tmux=tmux)
    lc = config.LevelConfig(
        level="L3", model=model, runtime="claude-code", role_variant="L3",
        tool_manifest=("read", "write", "edit", "bash", "task"),
    )
    result = adapter.pin_and_open({"role_variant": "L3"}, lc, "proj/x#exec", env or _iso_env())
    return list(result.argv), tmux


def test_system_prompt_file_argv_is_absolute():
    """--system-prompt-file resolves against the harness root (it must survive ANY pane cwd —
    the pane now boots in the node workspace, not the repo root)."""
    argv, _tmux = _adapter_argv()
    spf = argv[argv.index("--system-prompt-file") + 1]
    assert os.path.isabs(spf), f"--system-prompt-file must be ABSOLUTE; got {spf!r}"
    assert spf.endswith(".identity-prompt.md"), (
        f"the absolute path must still be the ONE shared prompt; got {spf!r}"
    )


def test_model_flag_derived_from_level_config():
    """argv carries the exact Opus 5 id proved live on pinned CC 2.1.152."""
    argv, _tmux = _adapter_argv(model="opus-5.0")
    assert "--model" in argv, "argv must carry --model derived from level_config.model"
    assert argv[argv.index("--model") + 1] == "claude-opus-5", (
        f"the spec's 'opus-5.0' maps to the PROBED CC id 'claude-opus-5'; got "
        f"{argv[argv.index('--model') + 1]!r}"
    )


def test_unknown_model_adds_no_model_flag():
    """A model with no CC mapping (e.g. the Codex-runtime 'gpt-5.5' driven through this adapter
    in tests) adds NO --model flag — the mapping is explicit, never guessed."""
    argv, _tmux = _adapter_argv(model="gpt-5.5")
    assert "--model" not in argv


# ===========================================================================
# (8) Deterministic trust for UNJAILED spawns — seed the actual pane cwd.
# ===========================================================================

def test_unjailed_spawn_seeds_trust_for_the_pane_cwd(tmp_path):
    """An unjailed spawn with a workspace must pre-seed trust for THAT cwd — otherwise the agent
    freezes on the trust dialog the moment the pane boots somewhere new."""
    cfg_dir = tmp_path / "cc-config"
    cfg_dir.mkdir()
    ws = tmp_path / "node-ws"
    ws.mkdir()

    tmux = _KickoffTmux()
    adapter = ClaudeCodeAdapter(tmux=tmux)
    lc = config.LevelConfig(
        level="L3", model="opus-5.0", runtime="claude-code", role_variant="L3",
        tool_manifest=("read", "write", "edit", "bash", "task"),
    )
    result = adapter.pin_and_open(
        {"role_variant": "L3", "workspace": str(ws)}, lc, "proj/x#exec",
        _iso_env(config_dir=str(cfg_dir)),
    )
    assert result.ok

    cj = json.loads((cfg_dir / ".claude.json").read_text(encoding="utf-8"))
    proj = cj.get("projects", {}).get(str(ws))
    assert proj and proj.get("hasTrustDialogAccepted") is True, (
        f"seed_trust must cover the ACTUAL pane cwd {ws} on an UNJAILED spawn; .claude.json: {cj!r}"
    )
    assert cj.get("bypassPermissionsModeAccepted") is True


def test_kickoff_pointer_carries_plan_first(runtime):
    """The durable kickoff row carries plan-first direction; the nudge only points at that row."""
    result, tmux, ws = _drive_real_spawn(runtime)
    assert result.ok
    assert tmux.sent, "the kickoff nudge must be typed"
    inbox = addressing.inbox_path(LEAF, runtime)
    kick = [json.loads(l) for l in inbox.read_text(encoding="utf-8").splitlines() if l.strip()]
    text = next(l["message"] for l in kick if l.get("type") == "kickoff")
    assert "native task list" in text and "bounded orientation" in text, (
        f"the durable kickoff row must carry the task-list-first discipline; got {text!r}"
    )
    assert "discover only that tool family" in text, (
        f"the kickoff must handle deferred native task-list tools; got {text!r}"
    )
    assert text.index("native task list") < text.index("brief.md"), (
        f"the kickoff must steer native task-list creation before brief orientation; got {text!r}"
    )
    assert "plan.md" in text and "report.md" in text, (
        f"the kickoff must carry the durable plan/report tail; got {text!r}"
    )
    assert "file-read tool" in text, (
        f"the kickoff must steer preinstantiated form edits through the file-read surface; got {text!r}"
    )
    _target, nudge = tmux.sent[0]
    assert "1 new message: 1 from harnessd" in nudge
    assert "native task list" not in nudge, "the phone-style nudge never duplicates kickoff content"
    assert any("plan.md" in (l.get("message") or "") for l in kick), (
        "the DURABLE kickoff line carries the same task-list-first direction"
    )
    assert any("file-read tool" in (l.get("message") or "") for l in kick), (
        "the DURABLE kickoff line carries the same preinstantiated-form edit guidance"
    )
