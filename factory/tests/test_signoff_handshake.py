"""F19 — the sign-off HANDSHAKE (finding detector_signals-1): the agent must be able to write
exactly what the daemon reads.

The daemon's fenced reader (detector_signals.read_terminal_signal) accepts a terminal-signal
artifact ``<node-dir>/.signal.<seat>.json`` ONLY when its ``owner_token`` equals the LIVE binding's
owner_token — but nothing delivered that token to the live agent: the brief payload omits it,
brief.md is pre-authored at plan time BEFORE the claim mints the token, and the unjailed pane env
is contractually EXACTLY the 4 isolation vars (no env delivery). F19 closes the gap with a
per-incarnation HANDSHAKE file ``<node-dir>/.sign-off.<seat>.json`` written by the chokepoint
strictly AFTER the claim commits (the POST-claim re-minted token) and strictly BEFORE
adapter.pin_and_open, carrying {owner_token, signal_path, schema}.

Load-bearing properties (each pins a mutant a wrong impl must fail on):
  * the handshake carries the POST-claim re-minted token, equal to the live binding's (mutant:
    write the pre-claim REGISTERED token -> the fence rejects the agent's sign-off -> caught);
  * a LOST claim writes NO handshake and never clobbers the live incarnation's (F-024 ordering:
    write strictly post-claim-commit);
  * a re-claim (the §6.4 resume through the SAME chokepoint) REFRESHES the handshake; the prior
    incarnation's token is fenced by the reader (the fence holds against the handshake copy);
  * the file is seat-qualified (.sign-off.review.json for a #review address — the L5/L5+ pair
    sharing one node dir don't clobber) and written via store.atomic_replace (no tmp residue);
  * the agent-facing docs state the daemon-read schema (the doc-drift guard pinning the exact
    3-way drift this finding caught — code vs spec vs role docs).

BIAS TO REAL (Lesson 7): REAL ledger under tmp_path RUNTIME_ROOT, REAL executor/claim CAS, REAL
chokepoint; only the RuntimeAdapter is faked (no model, no pane) — the test_chokepoint.py style.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.detector_signals as detector_signals
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.spawn.chokepoint as chokepoint
import harnessd.store as store


REPO_ROOT = Path(__file__).resolve().parents[1]

NODE = "proj/widget#exec"
REVIEW_NODE = "proj/widget#review"


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


class FakeAdapter:
    """The recorder fake (test_chokepoint.py style) — the ONLY mock; everything else is real."""

    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        from harnessd.spawn.adapters.base import SpawnResult

        self.calls.append((neutral_brief, level_config, tmux_target, env))
        return SpawnResult(
            ok=True,
            session_uuid="sess-uuid-spawned-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L5"),
            system_prompt_file=getattr(level_config, "system_prompt_file", "operational/shared/system-prompt.md"),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path="/tmp/sess-uuid-spawned-0001.jsonl",
            failure_class=None,
            launch_packet_file=neutral_brief.get("launch_packet_file"),
            launch_packet_hash=neutral_brief.get("launch_packet_hash"),
            launch_surface_source_hash=neutral_brief.get("launch_surface_source_hash"),
            reference_map_file=neutral_brief.get("reference_map_file"),
            reference_map_hash=neutral_brief.get("reference_map_hash"),
            reference_map_json_file=neutral_brief.get("reference_map_json_file"),
            launch_surface_version=neutral_brief.get("launch_surface_version"),
        )


def _install_adapter():
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    return fake


def _seed_planned(address=NODE, *, level="L5", generation=0, lease_epoch=1):
    """Register a fresh planned slot directly through the REAL ledger (the §2.6 seeding path)."""
    token = fencing.mint_owner_token(address, "subagent-x", "sess-uuid-seed", lease_epoch)
    rec = {
        "node_address": address,
        "parent_address": "proj#exec",
        "level": level,
        "subagent_id": "subagent-x",
        "session_uuid": "sess-uuid-seed",
        "state": "planned",
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "spec_pointer": "design/intent-spec.md",  # E1 fixture completion
        "frozen_acceptance_ref": "acceptance.md",  # E1 fixture completion
        "liveness_state": "claimed",
        "gate_crossed_at": None,
        "paused_at": None,
        "tmux_target": "harness:" + address,
        "workspace": str(addressing.node_dir(address, ledger.RUNTIME_ROOT)),
    }
    live = dict(ledger.all_nodes())
    live[address] = copy.deepcopy(rec)
    ledger.write_binding(live, _lock_held=True)
    return rec, token


def _level_config(level="L5"):
    return config.LevelConfig.for_level(level)


def _signoff_path(address, runtime):
    return addressing.node_dir(address, runtime) / f".sign-off.{addressing.split_address(address)[1]}.json"


def _spawn(address=NODE, *, level="L5"):
    """Seed planned + claim_and_spawn through the REAL chokepoint; return (registered_token, result)."""
    _, registered_token = _seed_planned(address, level=level)
    result = chokepoint.claim_and_spawn(
        address,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=registered_token,
        level_config=_level_config(level),
    )
    return registered_token, result


def _write_signal(address, runtime, *, signal, owner_token):
    p = addressing.signal_path(address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"signal": signal, "ts": "2026-06-10T00:00:00+00:00",
                    "owner_token": owner_token, "evidence": {"report": "report.md"}}),
        encoding="utf-8",
    )


# =========================================================================== #
# 1. The handshake carries the POST-claim re-minted token (MUTANT KILL: the
#    registered pre-claim token would pass a sloppier equality).
# =========================================================================== #

def test_claim_and_spawn_writes_handshake_with_post_claim_token(runtime):
    _install_adapter()
    registered_token, result = _spawn()
    assert result.ok

    handshake_path = _signoff_path(NODE, runtime)
    assert handshake_path.name == ".sign-off.exec.json"
    assert handshake_path.exists(), "claim_and_spawn must seed <node-dir>/.sign-off.<seat>.json"

    handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
    live = ledger.read_binding(NODE)
    assert handshake["owner_token"] == live["owner_token"], (
        "the handshake must carry the LIVE binding's owner_token (the one the fence validates)"
    )
    # MUTANT KILL: the claim RE-MINTS the token at a bumped epoch — an impl that writes the
    # pre-claim REGISTERED token hands the agent a token the fence will reject.
    assert handshake["owner_token"] != registered_token, (
        "the handshake must carry the POST-claim re-minted token, not the registered one"
    )
    assert handshake["signal_path"] == str(addressing.signal_path(NODE, runtime)), (
        "the handshake must name the exact absolute signal path the daemon reads"
    )
    # The schema block is the agent's self-contained write instruction.
    assert "schema" in handshake and "owner_token" in handshake["schema"]
    assert handshake["schema"]["signal"] == "DONE|FAILED"
    assert "ESCALATED" not in json.dumps(handshake["schema"])
    assert "needs_answer" in handshake["schema"]["evidence"]
    assert "park" in handshake["schema"]["evidence"]


def test_l5_spawn_materializes_launch_packet_before_adapter_open_and_records_metadata(runtime):
    fake = _install_adapter()
    node_dir = addressing.node_dir(NODE, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "brief.md").write_text(
        "# Task\n\nImplement the bounded smoke slice. Coordinate with L4 if ambiguous.\n",
        encoding="utf-8",
    )
    (node_dir / "acceptance.md").write_text(
        "# Acceptance\n\n- The smoke slice follows the launch packet contract.\n",
        encoding="utf-8",
    )

    _registered_token, result = _spawn()

    assert result.ok
    adapter_brief = fake.calls[0][0]
    launch_file = Path(adapter_brief["launch_packet_file"])
    reference_file = Path(adapter_brief["reference_map_file"])
    reference_json = Path(adapter_brief["reference_map_json_file"])

    assert launch_file.is_file()
    assert reference_file.is_file()
    assert reference_json.is_file()
    launch = launch_file.read_text(encoding="utf-8")
    assert "Implement the bounded smoke slice" in launch
    assert "Terminal signal JSON uses `signal`, not `status`" in launch
    assert "exact current UTC instant immediately before writing the signal" in launch
    assert "do not invent, round, or copy an example timestamp" in launch

    live = ledger.read_binding(NODE)
    assert live["launch_packet_file"] == str(launch_file)
    assert live["launch_packet_hash"] == adapter_brief["launch_packet_hash"]
    assert live["launch_surface_source_hash"] == adapter_brief["launch_surface_source_hash"]
    assert live["reference_map_file"] == str(reference_file)
    assert live["reference_map_json_file"] == str(reference_json)
    assert live["launch_surface_version"] == "launch-surface-v1"


def test_handshake_token_passes_the_real_fence(runtime):
    """The end-of-the-line proof at the reader: a signal written with the HANDSHAKE token is
    returned by the REAL fenced reader (the agent can write what the daemon reads)."""
    _install_adapter()
    _, result = _spawn()
    assert result.ok

    handshake = json.loads(_signoff_path(NODE, runtime).read_text(encoding="utf-8"))
    _write_signal(NODE, runtime, signal="DONE", owner_token=handshake["owner_token"])

    live = ledger.read_binding(NODE)
    sig = detector_signals.read_terminal_signal({"node_address": NODE}, live)
    assert sig is not None and sig["signal"] == "DONE", (
        "a signal carrying the handshake-delivered token must pass the live-binding fence"
    )


# =========================================================================== #
# 2. A LOST claim writes NO handshake — and never clobbers the live one.
# =========================================================================== #

def test_handshake_written_after_claim_never_on_lost_claim(runtime):
    _install_adapter()
    _, registered_token = _seed_planned(NODE)

    # Lose the claim via a WRONG CAS precondition (a stale generation — the F-024 shape).
    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=7,  # wrong: the slot is at generation 0
        expected_owner_token=registered_token,
        level_config=_level_config(),
    )
    assert not result.ok and result.failure_class == "claim_lost"
    assert not _signoff_path(NODE, runtime).exists(), (
        "a LOST claim must write NO handshake (write strictly post-claim-commit, F-024 ordering)"
    )


def test_lost_claim_does_not_clobber_the_live_incarnations_handshake(runtime):
    _install_adapter()
    _, registered_token = _seed_planned(NODE)

    # The live incarnation's handshake is already on disk (a prior successful claim wrote it).
    handshake_path = _signoff_path(NODE, runtime)
    handshake_path.parent.mkdir(parents=True, exist_ok=True)
    live_content = {"owner_token": "LIVE-INCARNATION-TOKEN", "signal_path": "x", "schema": {}}
    handshake_path.write_text(json.dumps(live_content), encoding="utf-8")

    result = chokepoint.claim_and_spawn(
        NODE,
        expected_state="planned",
        expected_generation=7,  # CAS miss -> ClaimLost
        expected_owner_token=registered_token,
        level_config=_level_config(),
    )
    assert not result.ok
    assert json.loads(handshake_path.read_text(encoding="utf-8")) == live_content, (
        "a lost claim must not touch the live incarnation's handshake"
    )


# =========================================================================== #
# 3. A re-claim (resume through the SAME chokepoint) REFRESHES the handshake;
#    the prior incarnation's token is fenced.
# =========================================================================== #

def test_reclaim_refreshes_handshake_and_old_token_is_fenced(runtime):
    _install_adapter()
    _, result = _spawn()
    assert result.ok

    handshake_path = _signoff_path(NODE, runtime)
    token_t1 = json.loads(handshake_path.read_text(encoding="utf-8"))["owner_token"]
    live_t1 = ledger.read_binding(NODE)
    assert live_t1["state"] == "running"

    # The §6.4 re-claim: resume the live address through the SAME chokepoint (bumps the epoch,
    # re-mints the token, and flows through the SAME _spawn_after_claim write-point).
    resumed = chokepoint.resume(
        NODE,
        expected_state="running",
        expected_generation=live_t1["generation"],
        expected_owner_token=token_t1,
        delta_inputs={},
        level_config=_level_config(),
    )
    assert resumed.ok

    token_t2 = json.loads(handshake_path.read_text(encoding="utf-8"))["owner_token"]
    assert token_t2 != token_t1, "the re-claim must REFRESH the handshake with the new token"
    live_t2 = ledger.read_binding(NODE)
    assert token_t2 == live_t2["owner_token"]

    # The prior incarnation signs off with its (handshake-copied) T1 -> FENCED (None).
    _write_signal(NODE, runtime, signal="DONE", owner_token=token_t1)
    assert detector_signals.read_terminal_signal({"node_address": NODE}, live_t2) is None, (
        "the prior incarnation's handshake-copied token must be fenced after the re-claim"
    )

    # The fresh incarnation signs off with T2 -> returned.
    _write_signal(NODE, runtime, signal="DONE", owner_token=token_t2)
    sig = detector_signals.read_terminal_signal({"node_address": NODE}, live_t2)
    assert sig is not None and sig["signal"] == "DONE"


# =========================================================================== #
# 4. Seat-qualified + atomic.
# =========================================================================== #

def test_handshake_is_seat_qualified_and_atomic(runtime, monkeypatch):
    _install_adapter()

    # Record atomic_replace calls while delegating to the REAL primitive (behavior unchanged).
    recorded = []
    real_atomic_replace = store.atomic_replace

    def _recording(path, render_fn):
        recorded.append(Path(path))
        return real_atomic_replace(path, render_fn)

    monkeypatch.setattr(store, "atomic_replace", _recording)

    _, result = _spawn(REVIEW_NODE)
    assert result.ok

    node_dir = addressing.node_dir(REVIEW_NODE, runtime)
    review_handshake = node_dir / ".sign-off.review.json"
    assert review_handshake.exists(), "a #review address gets .sign-off.review.json"
    assert not (node_dir / ".sign-off.exec.json").exists(), (
        "the #review seat must not clobber the exec seat's handshake (seat-qualified, shared node dir)"
    )

    assert review_handshake in recorded, (
        "the handshake must be written via store.atomic_replace (the atomic tmp+rename primitive)"
    )
    residue = list(node_dir.glob("*.tmp")) + list(node_dir.glob(".*.tmp"))
    assert residue == [], f"no tmp residue may remain in the node dir after the write: {residue}"


# =========================================================================== #
# 5. The doc-drift guard — the agent-facing docs state the DAEMON-READ schema
#    (pins the exact 3-way drift detector_signals-1 caught; DEFERRED-REGISTER
#    lesson 6's mock-to-real spirit, applied doc-to-code).
# =========================================================================== #

def test_comms_protocol_doc_states_the_daemon_read_schema():
    comms = (REPO_ROOT / "operational" / "shared" / "comms-protocol.md").read_text(encoding="utf-8")
    assert "owner_token" in comms, "comms-protocol must name the owner_token fence field"
    assert ".signal." in comms, "comms-protocol must name the .signal.<seat>.json artifact"
    assert ".sign-off." in comms, "comms-protocol must name the .sign-off.<seat>.json token source"
    assert "tmp+rename" in comms, "comms-protocol must instruct the atomic tmp+rename write"


def test_design_specs_no_longer_state_the_stale_schema():
    daemon_md = (REPO_ROOT / "design" / "DAEMON.md").read_text(encoding="utf-8")
    assert "{tag:" not in daemon_md, "DAEMON.md must not state the stale {tag, at, notes, session_uuid} schema"
    assert "session_uuid}" not in daemon_md, (
        "DAEMON.md §3.5 must fence on owner_token, not session_uuid (the stale spec schema)"
    )
    transports = (REPO_ROOT / "design" / "TRANSPORTS.md").read_text(encoding="utf-8")
    assert "validating `session_uuid`" not in transports, (
        "TRANSPORTS.md §2.4 must restate the owner_token fence, not session_uuid"
    )
    watchdog_md = (REPO_ROOT / "design" / "WATCHDOG.md").read_text(encoding="utf-8")
    assert "fences verdict-artifact journaling by `session_uuid`" not in watchdog_md, (
        "WATCHDOG.md §7 must derive the torn-verdict safety from the owner_token fence"
    )
