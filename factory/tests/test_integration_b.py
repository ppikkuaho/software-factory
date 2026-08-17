"""Increment 14 — Integration B (spawn -> detect -> sign-off -> collapse) FROZEN acceptance.

THE END-TO-END ARC, wired with the REAL modules (Increments 5/6/7/10/11):
    chokepoint.claim_and_spawn  -> the REAL ClaudeCodeAdapter argv/env ASSEMBLY (pin_and_open)
                                    -> tmux.create_detached (the ONLY mock — the tmux boundary)
    detector_signals.read_terminal_signal (the REAL fenced .signal.json reader)
    watchdog.check_leaf         -> the REAL terminal-signal-FIRST collapse
    chokepoint.collapse         -> the REAL single-writer executor (running -> done)
    ledger.load_wal             -> the REAL WAL arc, all actor=harnessd

Authoritative (grounded, not recalled — Lesson 4):
  - IMPLEMENTATION-PLAN Increment-14 Done-test (L805-817) + Integration B (L630-639)
    + DONE_WHEN clause 3 (L14-19, "one agent is spawned -> detected -> signed-off ->
    collapsed through the single writer with fencing active").
  - harnessd/spawn/chokepoint.py (claim_and_spawn / collapse),
    harnessd/spawn/adapters/claude_code.py (the REAL argv/env assembly path),
    harnessd/detector_signals.py (read_terminal_signal — the FENCED reader),
    harnessd/watchdog.py (check_leaf — STEP A terminal-signal FIRST),
    harnessd/executor.py (the single writer + the fencing precondition),
    harnessd/ledger.py (load_wal), harnessd/fencing.py.

BIAS TO REAL (Lesson 7): everything between claim and collapse is the REAL single-writer path.
The ONLY mock is ``tmux.create_detached`` (the tmux boundary; the done-test STOPS here and
ASSERTS create_detached is called with the fully-assembled REAL argv/env). The agent's
.signal.json is a REAL file the test writes as the fake CLI would. NO model call, NO real pane.

This is mostly an INTEGRATION TEST: if the real wiring already supports the arc it may go GREEN
immediately (that is fine — it PINS the end-to-end contract). The tests are written to FAIL if
any wiring/assembly is wrong (the LOAD-BEARING properties each kill their mutant).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

import harnessd.config as config
import harnessd.detector as detector
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.watchdog as watchdog
from harnessd.spawn import chokepoint
from harnessd.spawn.adapters.claude_code import ClaudeCodeAdapter


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so the REAL executor's
# pathless ledger calls (read_binding / append_wal / write_binding), the EX lock
# (.harnessd.lock), AND detector_signals' .signal.json resolution all land under
# tmp_path. Also clear the detector_signals size cache (Inc 6 private state).
# ===========================================================================

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    # Leave the chokepoint adapter seam clean between tests.
    previous_adapter = chokepoint.ADAPTER
    yield tmp_path
    chokepoint.set_adapter(previous_adapter)


# ===========================================================================
# The ONLY mock: a recording tmux at the create_detached boundary.
#
# It is injected into the REAL ClaudeCodeAdapter (adapter.tmux), so the adapter's
# REAL argv/env ASSEMBLY runs (build_pane_argv -> oauth gate -> create_detached)
# and we capture the fully-assembled (session_name, pane_argv, env) the adapter
# would have handed to the real tmux. server_env() is clean (no leaked key).
# ===========================================================================

class RecordingTmux:
    """Records create_detached args; never touches a real tmux server. Mirrors the REAL
    tmux wrapper's from-empty ``build_pane_argv`` seam so the adapter assembles the exact
    pane it would in production — but NO real exec happens (no pane process opens)."""

    def __init__(self):
        self.created = []  # list of (session_name, pane_argv, env)

    def build_pane_argv(self, env, argv):
        pane = ["env", "-i"]
        for key, value in env.items():
            pane.append(f"{key}={value}")
        pane += list(argv)
        return pane

    def create_detached(self, session_name, pane_argv, env, cwd=None):
        self.created.append((session_name, list(pane_argv), dict(env)))
        # Post-F18 contract (mirrors the real wrapper): return the CANONICAL live target.
        return f"{session_name}:0.0"

    def server_env(self):
        return {}  # a clean server — no leaked ANTHROPIC/OPENAI key

    def capture_pane(self, session_name):
        return ""

    def list_targets(self):
        return {}

    def kill(self, session_name):
        pass


# ===========================================================================
# Seeding helpers — a fresh ``planned`` binding (the start of the arc), the REAL
# write_binding seeding path, the REAL .signal.json the agent "writes".
# ===========================================================================

NODE = "proj/widget#exec"
PARENT = "proj#exec"
SUBAGENT = "subagent-intb-0001"
SESSION = "sess-uuid-intb-0001"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _planned_binding(
    *,
    node_address=NODE,
    parent_address=PARENT,
    generation=0,
    lease_epoch=1,
    subagent_id=SUBAGENT,
    session_uuid=SESSION,
    level="L3",
):
    """A fresh ``planned`` binding (the slot the chokepoint claims-before-spawn).

    Carries every field the REAL executor / detector / watchdog read, mirroring the
    suite-wide binding shape. owner_token is minted from the seed identity at lease_epoch.
    """
    token = fencing.mint_owner_token(node_address, subagent_id, session_uuid, lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "state": "planned",
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "spec_pointer": "design/intent-spec.md",  # E1 fixture completion
        "frozen_acceptance_ref": "acceptance.md",  # E1 fixture completion
        "liveness_state": "idle",
        "last_progress_at": None,
        "last_inbox_acked_offset": 0,
        "stale_check_count": 0,
        "stale_grace_checks": 2,
        "recovery_attempts": 0,
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": None,
        "terminal_signal": None,
        "suspicion_window_key": "working",
    }
    return rec, token


def _seed(bindings):
    ledger.write_binding(
        {b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True
    )


def _read(node=NODE):
    return ledger.read_binding(node)


def _level_config(role_variant="L3"):
    """A LevelConfig with a non-default role_variant so 'role rides the brief, not argv' is testable."""
    return config.LevelConfig(
        level=role_variant,
        model="opus-4.8",
        runtime="claude-code",
        role_variant=role_variant,
        tool_manifest=("read", "write", "edit", "bash", "task"),
    )


import harnessd.addressing as _addressing


def _node_dir(runtime, node_address):
    d = _addressing.node_dir(node_address, runtime)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_signal(runtime, node_address, *, signal, owner_token, evidence=None):
    """Write the REAL per-seat .signal.<seat>.json the agent would write, through the canonical
    addressing derivation (the same nested path the fenced reader uses)."""
    p = _addressing.signal_path(node_address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signal": signal,
        "ts": _now_iso(),
        "owner_token": owner_token,
        "evidence": evidence or {},
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _node_view(binding):
    """The node dict the watchdog/detector readers consume (node_address + transcript + target)."""
    return {
        "node_address": binding["node_address"],
        "transcript_path": binding.get("transcript_path"),
        "tmux_target": binding.get("tmux_target", "harness:t"),
    }


def _wal_events_for(node_address):
    """The ordered list of (event, from_state, to_state, actor) for this node's WAL rows."""
    return [
        (r.get("event"), r.get("from_state"), r.get("to_state"), r.get("actor"))
        for r in ledger.load_wal()
        if r.get("node_address") == node_address
    ]


def _drive_spawn(runtime, *, level_config=None):
    """Run the REAL end-to-end spawn arc (claim -> spawning -> running) and return (result, tmux).

    Wires the REAL ClaudeCodeAdapter (with a RecordingTmux at the create_detached boundary) as
    the chokepoint adapter, seeds a fresh ``planned`` binding, and runs claim_and_spawn. The ONLY
    mock is the tmux boundary; the executor/ledger/adapter-assembly/brief are all REAL.
    """
    tmux = RecordingTmux()
    adapter = ClaudeCodeAdapter(tmux=tmux)
    chokepoint.set_adapter(adapter)

    binding, token = _planned_binding()
    _seed([binding])

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token,
        level_config=level_config or _level_config(),
    )
    return result, tmux


# ===========================================================================
# (1) THE ASSEMBLY ASSERTION — the REAL adapter argv/env assembly is correct, and
#     create_detached received the fully-assembled real pane (the tmux boundary).
#
# LOAD-BEARING (each kills its mutant):
#   * argv == [CC, --system-prompt-file, operational/shared/system-prompt.md]  (SHARED constant,
#     NOT role.md)                              -> mutant: role.md in argv -> caught
#   * env == EXACTLY the 4 isolation vars        -> mutant: extra/missing env var -> caught
#   * pane is env -i from-empty isolation        -> mutant: no env-i isolation -> caught
#   * NO ANTHROPIC/OPENAI api key anywhere        -> mutant: an api key present -> caught
#   * the role rides the brief load-manifest, NOT the argv  -> mutant: role text in argv -> caught
# ===========================================================================

def test_create_detached_carries_the_fully_assembled_real_pane(runtime):
    """The arc reaches running AND create_detached saw the assembled real argv/env (the tmux boundary)."""
    result, tmux = _drive_spawn(runtime)

    assert getattr(result, "ok", False) is True, "the spawn arc must report a successful spawn"
    assert _read()["state"] == "running", "claim_and_spawn must land the node in running"

    # The tmux boundary fired EXACTLY once with the assembled pane (the done-test STOPS here).
    assert len(tmux.created) == 1, "create_detached must be called EXACTLY once (the tmux boundary)"
    session_name, pane_argv, env = tmux.created[0]

    # ---- the env-i from-empty isolation (the pane inherits NOTHING from the server) ----
    assert pane_argv[:2] == ["env", "-i"], (
        "the pane MUST be the from-empty `env -i` isolator (DAEMON §6.2) — the OAuth-only "
        f"mechanism; got {pane_argv[:4]!r}"
    )

    # ---- the COMPOSED identity prompt, NOT a bare per-level role path ----
    # The real argv (after the env -i K=V prefix) carries [CC, --system-prompt-file, <identity>].
    # ABSOLUTE since the transport increment (the pane boots in the node workspace, -c) — the
    # value resolves the ONE shared constant against HARNESS_ROOT (config.py's NOTE contract).
    import os as _os
    assert "--system-prompt-file" in pane_argv, "the boot MUST pass --system-prompt-file (H40 recipe)"
    spf = pane_argv[pane_argv.index("--system-prompt-file") + 1]
    assert _os.path.isabs(spf) and spf.endswith(".identity-prompt.md"), (
        "argv must carry the per-spawn COMPOSED identity bundle (identity auto-load, user "
        f"ruling 2026-06-12 / LR-4), NOT a bare per-level doc path; got {spf!r}"
    )

    # ---- NO H40 foot-guns in argv (--bare forces API-key auth; the others break role-as-documents) ----
    for flag in ("--bare", "--append-system-prompt", "--agents", "--agent"):
        assert flag not in pane_argv, f"argv must NEVER carry {flag!r} (H40 foot-gun); got {pane_argv!r}"

    # ---- the role rides the brief load-manifest, NOT the argv (role-as-documents) ----
    # No argv token may carry role/persona/role.md content — the ONLY path token is the shared prompt.
    joined = " ".join(pane_argv).lower()
    assert "role.md" not in joined, (
        "role-as-documents: the per-seat role rides the brief load-manifest, never the argv "
        f"(no role.md in argv); got {pane_argv!r}"
    )
    # Identity auto-load (2026-06-12): the system-prompt value is the per-spawn COMPOSED
    # bundle (it CONTAINS the role text); NO bare operational/* doc path may ride argv.
    op_paths = [tok for tok in pane_argv if "operational/" in tok]
    assert not op_paths, (
        "no bare operational/* doc path may appear in argv — the role rides the COMPOSED "
        f"identity bundle, never a doc pointer in argv; got {op_paths!r}"
    )
    spf_tok = pane_argv[pane_argv.index("--system-prompt-file") + 1]
    assert spf_tok.endswith(".identity-prompt.md")


def test_create_detached_env_is_exactly_the_four_isolation_vars_no_api_key(runtime):
    """The pane env is EXACTLY the 4 isolation vars — no extra var, no raw API key."""
    _result, tmux = _drive_spawn(runtime)
    assert tmux.created, "create_detached must have fired (the tmux boundary)"
    session_name, pane_argv, env = tmux.created[0]

    expected_keys = {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
        "PATH",  # LR-2 (2026-06-11 posture decision): non-credential ergonomics joined the floor
    }
    assert set(env) == expected_keys, (
        "the pane env MUST be the 4 isolation vars + PATH (DAEMON §6.2 amended by LR-2) — any OTHER "
        f"extra widens the from-empty pane; got {sorted(env)!r}"
    )

    # NO raw API key anywhere — neither in the env dict nor flattened into the env -i pane vector.
    for forbidden in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert forbidden not in env, (
            f"{forbidden} must NEVER be in the pane env (OAuth-only HARD INVARIANT); got {sorted(env)!r}"
        )
        assert not any(tok.startswith(forbidden + "=") for tok in pane_argv), (
            f"{forbidden} must NEVER be flattened into the env -i pane vector; got {pane_argv!r}"
        )

    # The OAuth token IS the auth channel (present, non-empty) — the positive half of the invariant.
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN"), (
        "the pane MUST carry CLAUDE_CODE_OAUTH_TOKEN (the OAuth/subscription auth channel)"
    )

    # The session name is recoverable from the address — the ONE canonical derivation (F18:
    # 'harness-' + the address with '/', '#', ':', '.' folded to '-'; the old 'harness:' prefix
    # was silently renamed by tmux, so the recorded key never matched the live session).
    from harnessd import addressing as _addressing
    assert session_name == _addressing.session_name_for(NODE), (
        f"session name must be addressing.session_name_for(address); got {session_name!r}"
    )


def test_role_delivered_via_brief_manifest_not_argv(runtime):
    """The per-seat role arrives as the brief's load-manifest (role-as-documents), not in argv.

    The REAL chokepoint assembles the brief (brief.assemble_neutral) and hands its load_manifest
    to the adapter; the adapter records role_variant on the result but NEVER bakes the role into
    argv. We assert the brief carried a non-empty load-manifest (the role docs to READ) AND that
    the argv carries no manifest content — the two halves of the role-as-documents contract.
    """
    result, tmux = _drive_spawn(runtime, level_config=_level_config("L3"))
    assert getattr(result, "ok", False) is True

    # role_variant is recorded as a FACT on the binding (config = intent), NOT in argv.
    final = _read()
    assert final.get("role_variant") == "L3", (
        "the seat's role_variant must be recorded on the binding (the per-seat fact); "
        f"got {final.get('role_variant')!r}"
    )

    # The assembled argv is identical regardless of role_variant: re-drive with L4 and compare.
    _session_a, pane_argv_a, _env_a = tmux.created[0]

    tmux_b = RecordingTmux()
    chokepoint.set_adapter(ClaudeCodeAdapter(tmux=tmux_b))
    binding_b, token_b = _planned_binding(node_address="proj/other#exec")
    _seed([_read(), binding_b])  # keep the first node, add a second
    chokepoint.claim_and_spawn(
        "proj/other#exec",
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=token_b,
        level_config=_level_config("L4"),
    )
    _session_b, pane_argv_b, _env_b = tmux_b.created[0]

    # The child argv is the portion AFTER the `env -i <K=V...>` prefix (the K=V tokens carry an '=';
    # the argv proper begins at the first non-K=V token: the CC binary path). Extract it for both.
    def _child_argv(pane_argv):
        for i, tok in enumerate(pane_argv[2:], start=2):  # skip the leading ["env", "-i"]
            if "=" not in tok:
                return pane_argv[i:]
        return []

    child_a = _child_argv(pane_argv_a)
    child_b = _child_argv(pane_argv_b)

    # The per-SPAWN ``--session-id <uuid>`` pair is the one sanctioned argv difference (it pins
    # CC's transcript file to the recorded session_uuid — 2026-06-11 live-run fix); strip it
    # before the identity check.
    def _strip_session_id(argv):
        argv = list(argv)
        i = argv.index("--session-id")
        argv = argv[:i] + argv[i + 2:]
        # identity auto-load (2026-06-12): the composed per-spawn prompt path is the second
        # sanctioned difference — pin its shape, strip the pair.
        i = argv.index("--system-prompt-file")
        assert argv[i + 1].endswith(".identity-prompt.md")
        return argv[:i] + argv[i + 2:]

    child_a = _strip_session_id(child_a)
    child_b = _strip_session_id(child_b)

    # The child argv MUST be byte-identical across role_variants L3/L4 (the shared prompt; the role
    # rides the brief). A mutant that bakes the role into argv (e.g. appends a per-level role path)
    # makes child_a != child_b -> caught here.
    assert child_a == child_b, (
        "the child argv must be identical across role_variants L3/L4 (the role is NEVER in argv — "
        f"role-as-documents); got L3={child_a!r} vs L4={child_b!r}"
    )
    # The composed-bundle shape itself is pinned inside _strip_session_id above (the pair is
    # stripped for the identity check — identity auto-load, 2026-06-12).


# ===========================================================================
# (2) THE FULL WAL ARC THROUGH THE SINGLE WRITER — claim -> spawn -> running -> done.
#
#   The agent signs off (REAL .signal.json DONE, live owner_token) -> the REAL watchdog
#   check_leaf reads the REAL fenced read_terminal_signal -> DONE -> COLLAPSE ->
#   chokepoint.collapse routes the terminal transition through the REAL executor.
#
# LOAD-BEARING: the WAL shows claim -> ... -> running -> done, ALL actor=harnessd.
#   * mutant: skip the collapse / write outside the executor -> the arc is incomplete -> caught
#   * mutant: the collapse does not reach the terminal 'done' state -> caught
# ===========================================================================

def test_full_arc_spawn_detect_signoff_collapse_through_single_writer(runtime):
    """One node: claim -> spawning -> running -> (signal DONE) -> collapse -> done; full WAL arc."""
    result, tmux = _drive_spawn(runtime)
    assert getattr(result, "ok", False) is True
    running = _read()
    assert running["state"] == "running", "the node must reach running before sign-off"

    live_token = running["owner_token"]  # the post-claim token (current epoch)

    # The agent signs off: a REAL .signal.json {DONE, owner_token=<current>} in the node dir.
    _write_signal(runtime, NODE, signal="DONE", owner_token=live_token, evidence={"report": "report.md"})
    # E2 fixture completion: the return contract requires report.md at DONE.
    _e2_report_dir = _addressing.node_dir(NODE, runtime)
    _e2_report_dir.mkdir(parents=True, exist_ok=True)
    (_e2_report_dir / "report.md").write_text("# report\n\ndone per brief.\n", encoding="utf-8")

    # The watchdog reads the REAL fenced reader -> DONE -> COLLAPSE through the REAL executor.
    action = watchdog.check_leaf(_node_view(running), running, now=_now_iso())

    # The action is a COLLAPSE (terminal-signal FIRST).
    tag = getattr(action, "kind", None) or getattr(action, "tag", None)
    assert tag == "COLLAPSE", (
        f"a fenced DONE signal must route to COLLAPSE (terminal-signal FIRST); got action={action!r}"
    )

    # The REAL executor landed the terminal collapse: the live binding is now `done`.
    after = _read()
    assert after["state"] == "done", (
        "the DONE collapse must route running->done through the REAL single-writer executor; "
        f"got state={after['state']!r}"
    )

    # ---- THE FULL WAL ARC, all by the single writer (actor=harnessd) ----
    arc = _wal_events_for(NODE)
    events = [e[0] for e in arc]

    # The claim, the spawn-open (claimed->spawning), the running confirm (spawning->running), and
    # the terminal collapse (running->done) are ALL present, in order, through the executor.
    assert "claim" in events, f"the WAL must record the claim (STEP1); got {events!r}"
    assert "spawn_open" in events, f"the WAL must record the spawn-open (claimed->spawning); got {events!r}"
    assert "spawn_running" in events, f"the WAL must record the running confirm (spawning->running); got {events!r}"

    # The terminal collapse row: running -> done, event signal_DONE (the §3.6 normative name).
    collapse_rows = [e for e in arc if e[2] == "done"]
    assert collapse_rows, f"the WAL must record the terminal collapse to 'done'; got {arc!r}"
    collapse_event, c_from, c_to, c_actor = collapse_rows[-1]
    assert c_from == "running" and c_to == "done", (
        f"the terminal collapse row must be running->done; got {collapse_rows!r}"
    )
    assert collapse_event == "signal_DONE", (
        f"the terminal collapse event must be the §3.6 normative signal_DONE; got {collapse_event!r}"
    )

    # Ordering: claim BEFORE spawn_open BEFORE spawn_running BEFORE the collapse (the arc is sequential).
    assert events.index("claim") < events.index("spawn_open") < events.index("spawn_running"), (
        f"the spawn arc rows must be in order claim->spawn_open->spawn_running; got {events!r}"
    )
    assert events.index("spawn_running") < len(events) - 1 or collapse_event in events, (
        "the collapse must come after the running confirm"
    )

    # EVERY row in the arc is stamped actor=harnessd (the single-writer stamp).
    actors = {e[3] for e in arc}
    assert actors == {"harnessd"}, (
        "EVERY WAL row in the arc must be by the single writer (actor=harnessd) — a row written "
        f"outside the executor would carry a different/absent actor; got {actors!r}"
    )


# ===========================================================================
# (3) FENCING BY BEHAVIOR (the load-bearing second half — NOT a presence check).
#
#   MID-ARC, advance the node epoch (simulate a re-adopt: a REAL executor.claim that
#   bumps lease_epoch + re-mints owner_token). Then:
#     * replay a transition presenting the OLD owner_token -> the executor JOURNALS
#       stale_return_ignored AND leaves the live binding UNCHANGED (de-authorized
#       non-destructively);
#     * a transition presenting the CURRENT-epoch token still COMMITS (the live owner
#       is unaffected).
#
# LOAD-BEARING:
#   * mutant: honor the stale token -> the binding changes -> caught (unchanged-binding assertion)
#   * mutant: fence everything -> the live owner cannot act -> caught (current-token commit assertion)
#   * mutant: do not journal the stale return -> caught (the stale_return_ignored WAL-row assertion)
# ===========================================================================

def test_fencing_by_behavior_stale_token_ignored_current_commits(runtime):
    """Mid-arc epoch advance: old-token replay -> stale_return_ignored + binding UNCHANGED;
    current-epoch token still commits (the live owner is unaffected)."""
    result, tmux = _drive_spawn(runtime)
    assert getattr(result, "ok", False) is True
    running = _read()
    assert running["state"] == "running"

    # The OLD incarnation's token (the live one BEFORE the mid-arc re-adopt).
    old_token = running["owner_token"]
    old_epoch = running["lease_epoch"]
    old_generation = running["generation"]

    # ---- MID-ARC: advance the node epoch via a REAL re-adopt (executor.claim from running) ----
    # claim() bumps lease_epoch + re-mints the owner_token in the SAME committed candidate, so the
    # prior incarnation is fenced out the instant this commits. (running -> claimed re-adopt edge.)
    re_adopt = executor.claim(
        NODE,
        expected_state="running",
        expected_generation=old_generation,
        expected_owner_token=old_token,
        level_config=_level_config(),
    )
    assert re_adopt.ok, f"the mid-arc re-adopt (running->claimed) must commit; got {re_adopt.errors!r}"

    readopted = _read()
    new_token = readopted["owner_token"]
    new_epoch = readopted["lease_epoch"]
    assert new_epoch == old_epoch + 1, "the re-adopt must BUMP the lease_epoch (fences the prior token)"
    assert new_token != old_token, "the re-adopt must RE-MINT the owner_token at the bumped epoch"

    # Snapshot the live binding BEFORE the stale replay (to prove byte-for-byte non-mutation).
    before_stale = copy.deepcopy(_read())
    wal_len_before = len(ledger.load_wal())

    # ---- STALE REPLAY: present the OLD token in a transition (the de-authorized incarnation) ----
    # Route it through the REAL single writer (executor.transition) — the path the watchdog/
    # chokepoint all funnel through. The OLD token must lose to the live (re-adopted) one.
    # Target a LEGAL edge out of 'claimed' (claimed->spawning), so the ONLY differentiator between
    # the stale replay and the live commit below is the owner_token fence — not a legality abort.
    stale = executor.transition(
        NODE,
        expected_state="claimed",
        expected_generation=readopted["generation"],
        expected_owner_token=old_token,  # the STALE, lower-epoch token
        target_state="spawning",
        binding_delta={"liveness_state": "working"},
        event="stale_replay_attempt",
        summary="a de-authorized prior incarnation replays with the old owner_token",
    )

    assert stale.ok is False, "the stale old-token transition must be REFUSED (fencing active)"

    # The live binding is UNCHANGED byte-for-byte (non-destructive de-authorization).
    after_stale = _read()
    assert after_stale == before_stale, (
        "a stale old-token replay must leave the live binding UNCHANGED (non-destructive "
        f"de-authorization, §3.6); before={before_stale!r} after={after_stale!r}"
    )

    # The stale return is JOURNALED as stale_return_ignored (an observed rejection, not silence).
    new_rows = ledger.load_wal()[wal_len_before:]
    stale_rows = [r for r in new_rows if r.get("event") == "stale_return_ignored"]
    assert stale_rows, (
        "the executor must JOURNAL a stale_return_ignored WAL row for the de-authorized replay "
        f"(fencing by behavior — an observed rejection); new rows={[r.get('event') for r in new_rows]!r}"
    )
    fenced = stale_rows[-1]
    assert fenced.get("node_address") == NODE
    assert fenced.get("from_state") == fenced.get("to_state"), (
        "the stale_return_ignored row must NOT advance state (from==to — nothing committed)"
    )
    assert fenced.get("owner_token") == new_token, (
        "the journaled row records the LIVE owner_token (the one that holds the slot), not the stale one"
    )

    # ---- THE CURRENT-EPOCH TOKEN STILL COMMITS (the live owner is unaffected by the fence) ----
    live_commit = executor.transition(
        NODE,
        expected_state="claimed",
        expected_generation=readopted["generation"],
        expected_owner_token=new_token,  # the CURRENT-epoch token
        target_state="spawning",  # the SAME legal edge the stale replay targeted — only the token differs
        binding_delta={"liveness_state": "working"},
        event="live_owner_acts",
        summary="the current-epoch owner acts and commits (fence does not block the live owner)",
    )
    assert live_commit.ok is True, (
        "the CURRENT-epoch token must still COMMIT (fence the STALE incarnation, not the live owner) — "
        f"a 'fence everything' mutant breaks this; got {live_commit.errors!r}"
    )
    committed = _read()
    assert committed["state"] == "spawning", "the live owner's transition (claimed->spawning) must commit"
    assert committed["liveness_state"] == "working", "the live owner's delta must be applied"
    assert committed["generation"] == readopted["generation"] + 1, (
        "the live commit must advance the per-node generation (a real committed transition)"
    )
