"""The OUTBOX operational seam — the IPC ``service-outbox`` route + the harnessctl subcommand.

The unit tests (test_outbox.py) pin the adjudication logic. THESE pin the two operational surfaces a
live cascade actually uses:
  - the daemon IPC handler ``service-outbox`` (one node, or all live non-leaf nodes), which drains the
    outbox THROUGH the real chokepoint (single writer) and returns a structured result;
  - the harnessctl ``service-outbox [--node X]`` subcommand that serializes the request (a CLIENT, not
    a writer — it never touches the ledger).
"""

import copy

import pytest

import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.harnessctl as harnessctl
import harnessd.spawn.chokepoint as chokepoint
import harnessd.spawn.outbox as outbox

PARENT = "proj/widget#exec"


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append(tmux_target)
        from harnessd.spawn.adapters.base import SpawnResult
        return SpawnResult(ok=True, session_uuid="s", model_used="m", role_variant="L3",
                           system_prompt_file="operational/shared/system-prompt.md",
                           system_prompt_file_hash="h", tmux_target=tmux_target,
                           transcript_path="/tmp/s.jsonl", failure_class=None)


def _install(fake):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
    else:
        chokepoint.ADAPTER = fake


import harnessd.addressing as _addressing


def _seed_live_node(runtime, address, level="L2"):
    token = fencing.mint_owner_token(address, "sa", "uuid", 2)
    workspace = _addressing.node_dir(address, runtime)  # NESTED (canonical)
    workspace.mkdir(parents=True, exist_ok=True)
    rec = {"node_address": address, "parent_address": "root#exec", "level": level, "subagent_id": "sa",
           "session_uuid": "uuid", "state": "running", "generation": 5, "lease_epoch": 2,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "working",
           "tmux_target": "harness:" + address, "workspace": str(workspace)}
    ledger.write_binding({address: copy.deepcopy(rec)}, _lock_held=True)
    return token, workspace


# --------------------------------------------------------------------------- #
# IPC route
# --------------------------------------------------------------------------- #

def test_service_outbox_is_a_known_ipc_command():
    assert "service-outbox" in ipc._DISPATCH


def test_ipc_service_outbox_one_node_spawns_the_child(runtime):
    _seed_live_node(runtime, PARENT)
    fake = _FakeAdapter(); _install(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]
    outbox.request_child_spawn(workspace, child_name="parser", child_level="L3", brief="design parser")

    resp = ipc.handle_request({"command": "service-outbox", "addr": PARENT})

    assert resp["ok"] and resp["spawned_count"] == 1 and resp["rejected_count"] == 0
    assert resp["serviced"][0]["child_address"] == "proj/widget/parser#exec"
    assert ledger.read_binding("proj/widget/parser#exec") is not None
    assert len(fake.calls) == 1


def test_ipc_service_outbox_no_addr_services_all(runtime):
    _seed_live_node(runtime, PARENT, level="L2")
    fake = _FakeAdapter(); _install(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]
    outbox.request_child_spawn(workspace, child_name="parser", child_level="L3", brief="x")

    resp = ipc.handle_request({"command": "service-outbox"})  # no addr -> sweep all

    assert resp["ok"] and resp["spawned_count"] == 1
    assert len(fake.calls) == 1


def test_ipc_service_outbox_reports_a_rejection(runtime):
    _seed_live_node(runtime, PARENT)
    fake = _FakeAdapter(); _install(fake)
    od = __import__("pathlib").Path(ledger.read_binding(PARENT)["workspace"]) / outbox.OUTBOX_DIRNAME
    od.mkdir(parents=True, exist_ok=True)
    (od / "0001-broken.json").write_text("{ not json", encoding="utf-8")

    resp = ipc.handle_request({"command": "service-outbox", "addr": PARENT})

    assert resp["ok"] and resp["spawned_count"] == 0 and resp["rejected_count"] == 1
    assert resp["serviced"][0]["status"] == "rejected" and resp["serviced"][0]["reason"]
    assert len(fake.calls) == 0


# --------------------------------------------------------------------------- #
# harnessctl subcommand (the CLIENT — serialize only, never a writer)
# --------------------------------------------------------------------------- #

def test_harnessctl_exposes_service_outbox():
    parser = harnessctl.build_parser()
    args = parser.parse_args(["service-outbox", "--node", PARENT])
    assert args.command == "service-outbox" and args.addr == PARENT
    request = harnessctl._build_request(args)
    assert request == {"command": "service-outbox", "addr": PARENT}


def test_harnessctl_service_outbox_node_optional():
    parser = harnessctl.build_parser()
    args = parser.parse_args(["service-outbox"])  # no --node -> sweep all
    assert args.command == "service-outbox" and args.addr is None
    request = harnessctl._build_request(args)
    assert request["command"] == "service-outbox" and request["addr"] is None


# --------------------------------------------------------------------------- #
# the WIRED daemon loop — poll_once drains outboxes (the live cascade seam)
# --------------------------------------------------------------------------- #

def test_daemon_poll_once_services_the_outbox_and_spawns_the_child(runtime):
    """The REAL daemon.poll_once (reconcile_tick + outbox drain) brings an agent's requested child
    online — the wired live-cascade loop, not the directly-called function. A parent agent drops a
    request; the next poll tick spawns its child."""
    from types import SimpleNamespace
    import harnessd.daemon as daemon
    import harnessd.executor as executor

    _seed_live_node(runtime, PARENT, level="L2")
    fake = _FakeAdapter(); _install(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]
    outbox.request_child_spawn(workspace, child_name="parser", child_level="L3", brief="design parser")

    # tmux must report the parent's pane ALIVE, else reconcile (correctly) necro's it as owned-but-dead
    # before the outbox is ever serviced. The live pane keeps the parent running so it can spawn.
    parent_target = ledger.read_binding(PARENT)["tmux_target"]

    class _Tmux:
        def list_targets(self):
            return {parent_target: {"pane_pid": 4242, "pane_dead": 0}}

    class _Detector:
        def liveness(self, node_address):
            return SimpleNamespace(state="working", last_progress_at=None)

    daemon.poll_once(executor, _Tmux(), _Detector())  # ONE real poll tick

    assert ledger.read_binding(PARENT)["state"] == "running", "the parent stays live (adopted, not necro'd)"
    child = ledger.read_binding("proj/widget/parser#exec")
    assert child is not None, "one poll tick must service the outbox and register the child"
    assert child["parent_address"] == PARENT, "the spawned child hangs under the requesting parent"
    assert len(fake.calls) == 1, "exactly one child actor opened by the poll tick"


def test_daemon_poll_once_survives_a_malformed_outbox_request(runtime):
    """A malformed request must not abort the reconcile sweep — the supervision tree keeps advancing
    (best-effort outbox servicing). The bad request is rejected, the tick still completes."""
    from types import SimpleNamespace
    import harnessd.daemon as daemon
    import harnessd.executor as executor

    _seed_live_node(runtime, PARENT, level="L2")
    fake = _FakeAdapter(); _install(fake)
    od = __import__("pathlib").Path(ledger.read_binding(PARENT)["workspace"]) / outbox.OUTBOX_DIRNAME
    od.mkdir(parents=True, exist_ok=True)
    (od / "0001-broken.json").write_text("{ not json", encoding="utf-8")

    parent_target = ledger.read_binding(PARENT)["tmux_target"]

    class _Tmux:
        def list_targets(self):
            return {parent_target: {"pane_pid": 4242, "pane_dead": 0}}

    class _Detector:
        def liveness(self, node_address):
            return SimpleNamespace(state="working", last_progress_at=None)

    daemon.poll_once(executor, _Tmux(), _Detector())  # must NOT raise

    assert list(od.glob("*.rejected")), "the malformed request is rejected, the tick still completed"
    assert len(fake.calls) == 0
