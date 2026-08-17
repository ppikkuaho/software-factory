"""Increment 12 — FROZEN acceptance for the daemon boot/orchestration entry (Integration A).
Tests ONLY — NO implementation. RED first.

This companion file pins the daemon's TOP-LEVEL wiring that test_genesis.py does not:
  * daemon.boot(runtime) -> None : lock + runtime.json + run_genesis (the §2.12 daemon entry).
    boot must (a) write the runtime.json descriptor (§2.3: build-id / started_at / pid), and (b) run
    genesis end-to-end so L1 is spawned in-role on first boot (Integration A) through the REAL
    chokepoint + REAL reconcile against the REAL on-disk ledger.
  * poll_loop factors one drivable iteration and composes it only while durable work is live.
    Completed-tick/idle-return behavior is tested in test_run_lifecycle.py; this file checks shape.

Authoritative sources:
  * IMPLEMENTATION-PLAN §2.12 (daemon.boot / poll_loop), §3 module table (daemon.py row, L64):
    "Acquires .harnessd.lock (single-instance), runs genesis, then reconcile_tick on a timer; writes
    the lock-free status sidecar." §3 on-disk tree (L459): runtime.json = the daemon runtime descriptor
    (build-id, started_at, pid).
  * DAEMON §2.2 (service-manager-hosted), §2.3 (runtime.json / status.json), §7 (the genesis sequence
    boot drives).

BIAS TO REAL: boot drives the REAL genesis -> REAL chokepoint -> REAL executor + on-disk ledger; the
ONLY fakes are the RuntimeAdapter (records the L1 spawn; dry-run) and tmux.list_targets. No daemon
process is launched; no real pane; no model.

NO IMPLEMENTATION here — harnessd/daemon.py does not exist yet (RED until written).

LOAD-BEARING:
  * boot writes the runtime.json descriptor (mutant: skip runtime.json -> caught).
  * boot runs genesis end-to-end -> L1 spawned in-role on first boot (mutant: boot does not run genesis
    -> no L1 actor opened -> caught).
  * poll_loop keeps its interval seam; bounded completed-tick/idle behavior is exercised elsewhere.
"""

from __future__ import annotations

import importlib
import inspect
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

import harnessd.config as config
import harnessd.executor as executor  # noqa: F401 (the REAL single writer genesis routes through)
import harnessd.ledger as ledger
from harnessd.spawn import chokepoint


def _daemon():
    return importlib.import_module("harnessd.daemon")


L1_ADDRESS = "L1#exec"


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


@pytest.fixture(autouse=True)
def _reset_chokepoint_adapter():
    previous = chokepoint.ADAPTER
    try:
        yield
    finally:
        chokepoint.ADAPTER = previous


def _spawn_result_cls():
    base = importlib.import_module("harnessd.spawn.adapters.base")
    return base.SpawnResult


class FakeAdapter:
    """Records pin_and_open; returns a happy dry-run SpawnResult (no real pane)."""

    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        SpawnResult = _spawn_result_cls()
        return SpawnResult(
            ok=True,
            session_uuid="sess-daemon-boot-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L1"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path="/runtime/transcripts/sess-daemon-boot-0001.jsonl",
            failure_class=None,
        )


class FakeTmux:
    def __init__(self, targets=None):
        self._targets = dict(targets or {})

    def list_targets(self):
        return dict(self._targets)


def _install_adapter(fake):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
    else:
        chokepoint.ADAPTER = fake


def _runtime_descriptor(runtime_root, *, fake_adapter, tmux):
    """Assemble the ``runtime`` descriptor boot(runtime) consumes.

    Permissive SimpleNamespace: carries the runtime_root, the genesis ``config`` (env + L1 address +
    pinned binary), and the fake adapter + fake tmux so boot can wire genesis without a real pane.
    The test owns the inputs; the daemon owns the sequence (lock -> runtime.json -> genesis).
    """
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-present",
        "CLAUDE_CONFIG_DIR": str(runtime_root / ".cc-pinned/config"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    cfg = SimpleNamespace(
        env=env,
        l1_address=L1_ADDRESS,
        l1_level="L1",
        runtime_root=runtime_root,
        build_id="build-test-0001",
        pinned_binary=config.PINNED_BINARY,
        level_config=config.LevelConfig.for_level("L1"),
    )
    return SimpleNamespace(
        runtime_root=runtime_root,
        build_id="build-test-0001",
        config=cfg,
        adapter=fake_adapter,
        tmux=tmux,
        executor=executor,
    )


def _l1_opens(fake):
    return sum(1 for (_b, _l, tgt, _e) in fake.calls if L1_ADDRESS in str(tgt))


# ===========================================================================
# boot writes runtime.json (the §2.3 daemon runtime descriptor).
# ===========================================================================

def test_boot_writes_runtime_json_descriptor(runtime):
    daemon = _daemon()
    fake = FakeAdapter()
    _install_adapter(fake)
    rt = _runtime_descriptor(runtime, fake_adapter=fake, tmux=FakeTmux({}))

    daemon.boot(rt)

    runtime_json = runtime / "runtime.json"
    assert runtime_json.is_file(), (
        "daemon.boot must write the runtime.json descriptor (§3 tree, §2.3: build-id/started_at/pid)"
    )
    data = json.loads(runtime_json.read_text())
    # The §2.3 runtime descriptor fields — at least one of the named keys must be present.
    assert any(k in data for k in ("build_id", "build-id", "started_at", "pid")), (
        "runtime.json must carry the §2.3 daemon runtime descriptor (build-id / started_at / pid)"
    )


# ===========================================================================
# boot runs genesis end-to-end -> L1 spawned in-role on first boot (Integration A).
# ===========================================================================

def test_boot_runs_genesis_and_spawns_l1_first_boot(runtime):
    daemon = _daemon()
    fake = FakeAdapter()
    _install_adapter(fake)
    rt = _runtime_descriptor(runtime, fake_adapter=fake, tmux=FakeTmux({}))

    daemon.boot(rt)

    assert _l1_opens(fake) == 1, (
        "daemon.boot must run genesis end-to-end so the L1 root is spawned in-role on first boot "
        "(Integration A) — exactly one L1 actor opened through the real chokepoint"
    )
    l1 = ledger.read_binding(L1_ADDRESS)
    assert l1 is not None and l1.get("state") == "running", (
        "after boot, the L1 root binding must be registered and running (the real chokepoint arc)"
    )
    assert l1.get("parent_address") in (None, ""), "the L1 root is parentless (DAEMON §7)"


# ===========================================================================
# poll_loop composes completed ticks until definitive idle. We assert its shape here;
# its single-iteration and idle-return behavior are exercised in focused tests.
# ===========================================================================

def test_poll_loop_keeps_the_timer_interval_seam(runtime):
    daemon = _daemon()
    assert hasattr(daemon, "poll_loop") and callable(daemon.poll_loop), (
        "the daemon must expose poll_loop(interval_s) — reconcile_tick on a timer while live"
    )
    sig = inspect.signature(daemon.poll_loop)
    assert "interval_s" in sig.parameters or len(sig.parameters) >= 1, (
        "poll_loop must take the timer interval (interval_s) — §2.12 poll_loop(interval_s)"
    )
    # The loop's completed-tick/idle return is tested in test_run_lifecycle.py.
