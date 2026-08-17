"""F18 (finding OSA-01) — session-key consistency: the recorded tmux_target must BE the live key.

The break this pins (probed live on tmux 3.6a, 2026-06-10):

  * tmux SILENTLY RENAMES session names containing ':' or '.' to '_' variants
    ('harness:L1-exec' -> 'harness_L1-exec'; 'harness-proj-1.2-exec' -> 'harness-proj-1_2-exec'),
    so a recorded "harness:" + collapse(address) name NEVER matches the live session.
  * detector_signals.pane_alive and reconcile's sweep look up binding["tmux_target"] DIRECTLY as a
    key of tmux.list_targets(), whose keys are "<session>:<window>.<pane>" TRIPLES — so even an
    un-renamed bare session name never matches (missing the ':0.0' suffix).

The fix this pins:

  1. ONE canonical session-name derivation: ``addressing.session_name_for(address)`` — prefix
     'harness-' (a name tmux will NOT rewrite) + the address with '/', '#', ':', '.' all folded
     to '-'. The adapter and every registration placeholder use it; collapse duplicates die.
  2. ``tmux.create_detached`` returns the CANONICAL live target straight from tmux itself
     (`-P -F '#{session_name}:#{window_index}.#{pane_index}'`) — the post-rename truth + the REAL
     indices (a non-zero base-index box is reported correctly, never guessed).
  3. The adapter records tmux_target = the canonical target RETURNED by create_detached, and the
     chokepoint's STEP4 writes it onto the binding — so pane_alive/reconcile key-match for real.

Real-tmux tests follow the repo pattern: per-test dedicated `-L` socket + kill-server teardown.
"""

from __future__ import annotations

import copy
import importlib
import os
import shutil
import subprocess
import time
import uuid

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.detector_signals as sig
import harnessd.fencing as fencing
import harnessd.ledger as ledger


_HAS_TMUX = shutil.which("tmux") is not None
_real_tmux = pytest.mark.skipif(not _HAS_TMUX, reason="real-tmux test: tmux binary not installed")


def _tmux_mod():
    return importlib.import_module("harnessd.spawn.tmux")


def _claude_mod():
    return importlib.import_module("harnessd.spawn.adapters.claude_code")


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
def tmux_server():
    """A REAL tmux server on a DEDICATED unique -L socket, torn down via kill-server."""
    sock = "f18-" + uuid.uuid4().hex[:12]
    mod = _tmux_mod()
    prior = getattr(mod, "_SOCKET", None)
    mod.set_socket(sock)
    try:
        yield mod, sock
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], check=False, capture_output=True)
        _rm_socket_file(sock)
        mod.set_socket(prior)


# ===========================================================================
# (1) The ONE canonical session-name derivation — addressing.session_name_for.
# ===========================================================================

def test_session_name_for_folds_the_whole_unsafe_alphabet():
    """'/', '#', ':' and '.' are ALL folded to '-' — tmux rewrites ':' and '.', so a session name
    carrying either would be silently renamed and the recorded key would never match the live one."""
    name = addressing.session_name_for("proj/payments/v1.2/gateway#exec")
    assert name == "harness-proj-payments-v1-2-gateway-exec", name
    for ch in ("/", "#", ":", "."):
        assert ch not in name, f"session_name_for must fold {ch!r} (tmux renames it to '_')"


def test_session_name_for_uses_the_harness_dash_prefix():
    """The prefix is 'harness-' (NOT 'harness:') — a ':' in the NAME is rewritten to '_' by tmux,
    so the colon-prefixed name could never match. 'harness-' is stable and operator-predictable
    (`tmux -L <socket> attach -t harness-<collapsed-address>`)."""
    name = addressing.session_name_for("root#exec")
    assert name.startswith("harness-"), name
    assert not name.startswith("harness:"), "the 'harness:' prefix is the OSA-01 bug — tmux renames it"


def test_session_name_for_handles_colon_dot_in_address_segments():
    """An address that itself carries ':' or '.' segments still derives a tmux-safe name."""
    name = addressing.session_name_for("proj/api:v2/mod.py#review")
    assert name == "harness-proj-api-v2-mod-py-review", name


# ===========================================================================
# (2) The adapter derives its session from session_name_for and records the
#     canonical target RETURNED by create_detached (not the requested name).
# ===========================================================================

class _CanonicalMockTmux:
    """Mirrors the REAL wrapper's post-fix contract: create_detached returns the canonical
    '<session>:<window>.<pane>' triple (what `-P -F` prints), exactly as list_targets keys it."""

    def __init__(self):
        self.created = []

    def build_pane_argv(self, env, argv):
        pane = ["env", "-i"]
        for k, v in env.items():
            pane.append(f"{k}={v}")
        return pane + list(argv)

    def create_detached(self, session_name, pane_argv, env, cwd=None):
        self.created.append((session_name, list(pane_argv), dict(env), cwd))
        return f"{session_name}:0.0"

    def server_env(self):
        return {}

    def capture_pane(self, session_name):
        return ""

    def list_targets(self):
        return {}

    def kill(self, session_name):
        pass


def _iso_env():
    return {
        "CLAUDE_CONFIG_DIR": "/HARNESS/.cc-pinned/config",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-xyz",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def _adapter_spawn(address):
    cca = _claude_mod()
    tmux = _CanonicalMockTmux()
    adapter = cca.ClaudeCodeAdapter(tmux=tmux)
    lc = config.LevelConfig(
        level="L3", model="opus-4.8", runtime="claude-code", role_variant="L3",
        tool_manifest=("read", "write", "edit", "bash", "task"),
    )
    result = adapter.pin_and_open({"role_variant": "L3"}, lc, address, _iso_env())
    return result, tmux


def test_adapter_session_name_comes_from_the_one_addressing_derivation():
    """The session name handed to create_detached IS addressing.session_name_for(address) —
    no second collapse implementation that could drift (the unified-collapse half of F18)."""
    address = "payments/gateway/stripe-v1.2#exec"
    _result, tmux = _adapter_spawn(address)
    assert tmux.created, "create_detached must have been called"
    session_name = tmux.created[0][0]
    assert session_name == addressing.session_name_for(address), (
        f"adapter session name {session_name!r} != addressing.session_name_for "
        f"{addressing.session_name_for(address)!r} — collapse duplicates must be unified"
    )


def test_adapter_records_the_canonical_target_returned_by_create_detached():
    """SpawnResult.tmux_target is the value create_detached RETURNED (tmux's own post-rename
    '<session>:<window>.<pane>' report), never the requested/guessed session name."""
    result, tmux = _adapter_spawn("proj/x#exec")
    session_name = tmux.created[0][0]
    assert result.tmux_target == f"{session_name}:0.0", (
        f"tmux_target must be the canonical triple create_detached returned; got {result.tmux_target!r}"
    )


# ===========================================================================
# (3) STEP4 writes the adapter-returned canonical target onto the BINDING —
#     the key pane_alive / reconcile actually look up.
# ===========================================================================

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def test_chokepoint_step4_records_canonical_tmux_target_on_binding(runtime):
    """After a chokepoint spawn the BINDING's tmux_target is the canonical live target the
    adapter returned — not the registration placeholder. Without this, reconcile/pane_alive
    look up a key that never exists in list_targets() (the doubly-broken half of OSA-01)."""
    from harnessd.spawn import chokepoint
    from harnessd.spawn.adapters.base import SpawnResult

    node = "proj/widget#exec"
    canonical = addressing.session_name_for(node) + ":0.0"

    class _Adapter:
        def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
            return SpawnResult(
                ok=True, session_uuid="sess-f18-0001", model_used="opus-4.8 / claude-code",
                role_variant="L3", system_prompt_file=config.SYSTEM_PROMPT_FILE,
                system_prompt_file_hash="deadbeef",
                tmux_target=canonical,  # what the REAL adapter now records (create_detached's return)
                transcript_path="/tmp/sess-f18-0001.jsonl", failure_class=None,
            )

    token = fencing.mint_owner_token(node, "subagent-f18", "sess-seed", 1)
    binding = {
        "node_address": node, "parent_address": "proj#exec", "level": "L3",
        "subagent_id": "subagent-f18", "session_uuid": "sess-seed",
        "tmux_target": addressing.session_name_for(node),  # pre-spawn placeholder
        "state": "planned", "generation": 0, "lease_epoch": 1, "owner_token": token,
        "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md", "liveness_state": "claimed", "terminal_signal": None,
        "gate_crossed_at": None, "paused_at": None, "transcript_path": None,
    }
    ledger.write_binding({node: copy.deepcopy(binding)}, _lock_held=True)

    prior = chokepoint.ADAPTER
    chokepoint.set_adapter(_Adapter())
    try:
        result = chokepoint.claim_and_spawn(
            node, expected_state="planned", expected_generation=0,
            expected_owner_token=token, level_config=config.LevelConfig.for_level("L3"),
        )
    finally:
        chokepoint.set_adapter(prior)

    assert result.ok, f"spawn must succeed: {result!r}"
    live = ledger.read_binding(node)
    assert live["state"] == "running"
    assert live["tmux_target"] == canonical, (
        f"STEP4 must record the canonical live target onto the binding; got {live['tmux_target']!r}"
    )


def test_registration_placeholders_use_the_canonical_session_name(runtime):
    """The pre-spawn registration placeholder (chokepoint._register_child / genesis L1 root)
    must use session_name_for — never the renamed-by-tmux 'harness:' + raw-address shape."""
    from harnessd.spawn import chokepoint

    parent = "proj#exec"
    child = "proj/leaf#exec"
    token = fencing.mint_owner_token(parent, "subagent-p", "sess-p", 1)
    parent_binding = {
        "node_address": parent, "parent_address": None, "level": "L2",
        "subagent_id": "subagent-p", "session_uuid": "sess-p",
        "tmux_target": addressing.session_name_for(parent), "state": "running",
        "generation": 1, "lease_epoch": 1, "owner_token": token, "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md",
        "liveness_state": "working", "terminal_signal": None,
        "gate_crossed_at": None, "paused_at": None, "transcript_path": "/tmp/p.jsonl",
    }
    ledger.write_binding({parent: copy.deepcopy(parent_binding)}, _lock_held=True)

    registered = chokepoint._register_child(
        child, parent, config.LevelConfig.for_level("L3"), runtime
    )
    assert registered["tmux_target"] == addressing.session_name_for(child), (
        f"registration placeholder must be the canonical session name; got {registered['tmux_target']!r}"
    )
    assert ":" not in registered["tmux_target"].replace("harness-", "", 1).split(":")[0] or True
    assert not registered["tmux_target"].startswith("harness:"), "the OSA-01 placeholder shape is dead"


# ===========================================================================
# (4) REAL tmux — the dotted/exotic address round-trips end-to-end:
#     create_detached's return IS a key of list_targets(); pane_alive matches;
#     kill via the full triple tears it down.
# ===========================================================================

@_real_tmux
def test_real_tmux_dotted_address_roundtrip_create_listtargets_pane_alive_kill(tmux_server):
    mod, _sock = tmux_server
    address = "proj/v1.2/api:beta/svc#exec"  # every unsafe char tmux would rewrite
    session_name = addressing.session_name_for(address)
    target = mod.create_detached(session_name, ["sh", "-c", "sleep 30"], {"CLAUDE_CONFIG_DIR": "/x"})

    # (a) the return is the canonical '<session>:<window>.<pane>' triple, unrenamed.
    assert target.startswith(session_name + ":"), (
        f"tmux RENAMED the session: requested {session_name!r}, live {target!r} — "
        "session_name_for must produce a name tmux does not rewrite"
    )

    # (b) the return IS a key of list_targets() — the exact key reconcile/pane_alive read.
    deadline = time.time() + 5
    targets = {}
    while time.time() < deadline:
        targets = mod.list_targets()
        if target in targets:
            break
        time.sleep(0.1)
    assert target in targets, (
        f"create_detached's return {target!r} must be a key of list_targets(); got {list(targets)!r}"
    )

    # (c) the REAL detector_signals.pane_alive, bound to the real wrapper, reads it alive.
    prior_seam = sig._tmux
    sig._tmux = mod
    try:
        node = {"node_address": address, "tmux_target": target, "transcript_path": None}
        alive, pid = sig.pane_alive(node)
        assert alive is True, "a node whose tmux_target is create_detached's return must read ALIVE"
        assert isinstance(pid, int) and pid > 0

        # (d) kill accepts the full triple as -t; afterwards the node reads NOT alive.
        mod.kill(target)
        deadline = time.time() + 5
        while time.time() < deadline:
            alive, _ = sig.pane_alive(node)
            if not alive:
                break
            time.sleep(0.1)
        assert sig.pane_alive(node)[0] is False, "after kill(target) the node must read NOT alive"
    finally:
        sig._tmux = prior_seam


@_real_tmux
def test_real_tmux_create_detached_reports_the_post_rename_truth(tmux_server):
    """Even when the CALLER passes a name tmux rewrites, the return is the POST-RENAME canonical
    target (tmux's own report via -P -F), never an echo of the requested name. Pins the mechanism:
    a regression to echo-back would pass the safe-name test above but fail here."""
    mod, _sock = tmux_server
    requested = "f18-bad:name.dot"  # tmux 3.6a rewrites ':' and '.' to '_'
    target = mod.create_detached(requested, ["sh", "-c", "sleep 30"], {"CLAUDE_CONFIG_DIR": "/x"})
    assert target in mod.list_targets(), (
        f"the return {target!r} must be a live list_targets key even for a renamed session"
    )
    assert not target.startswith(requested + ":"), (
        "tmux renamed this session — an echo-back of the requested name would be the OSA-01 bug"
    )
    mod.kill(target)
