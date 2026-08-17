"""F7 — the L1 root must carry a `workspace` field (REMEDIATION-PLAN-2026-06-07; review CFW-01).

The cascade edge-1 bug: `service_outbox` returns [] when a node has no `workspace` (outbox.py:219), and
`_register_l1_root` never set one — so the L1 root's outbox is never serviced and **L1 can never spawn
L2**. `_register_child` sets workspace (added in the nesting fix) but genesis was never updated. Masked by
the outbox tests hand-seeding workspace on every parent (CFW-03); this is the de-mask.
"""

import harnessd.addressing as addressing
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.genesis as genesis
import harnessd.spawn.outbox as outbox

import pytest


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


L1 = "L1#exec"


def test_register_l1_root_sets_the_workspace_field(runtime):
    """The registered L1 root binding carries `workspace` = its canonical nested node dir — so the L1
    outbox is serviceable (the cascade can begin)."""
    binding = genesis._register_l1_root(L1, "L1", "L1", runtime)
    expected = str(addressing.node_dir(L1, runtime))
    assert binding.get("workspace") == expected, "genesis must set the L1 root's workspace"
    # and it must round-trip through the ledger (not just the returned dict)
    assert ledger.read_binding(L1)["workspace"] == expected


def test_l1_outbox_is_serviceable_after_genesis_registration(runtime):
    """The load-bearing consequence: with the workspace set, the L1 root's outbox is READ (not
    short-circuited to []). A spawn-request dropped in L1's outbox is seen by the daemon — the L1->L2
    cascade edge is live. (Mutant: drop the workspace -> service_outbox returns [] -> the request is
    never seen -> CAUGHT.)"""
    binding = genesis._register_l1_root(L1, "L1", "L1", runtime)
    # mark L1 live (genesis registers it 'planned'; the cascade services a running node) + give it a real
    # workspace dir on disk where the outbox lives.
    b = ledger.read_binding(L1); b["state"] = "running"
    ledger.write_binding({L1: b}, _lock_held=True)
    workspace = binding["workspace"]
    addressing.node_dir(L1, runtime).mkdir(parents=True, exist_ok=True)
    # drop a (malformed) request: we only need to prove the outbox is READ, not that a spawn succeeds.
    od = addressing.node_dir(L1, runtime) / outbox.OUTBOX_DIRNAME
    od.mkdir(parents=True, exist_ok=True)
    (od / "0001-probe.json").write_text("{ not json", encoding="utf-8")

    outcomes = outbox.service_outbox(L1)

    assert outcomes != [], (
        "the L1 outbox must be READ (a request adjudicated) — an empty result means workspace is unset "
        "and the cascade is dead at edge 1"
    )
    assert outcomes[0].status == "rejected", "the malformed probe is adjudicated (proves the outbox was read)"
