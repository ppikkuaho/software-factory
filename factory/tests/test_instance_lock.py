"""F6 — the persistent single-instance lock (`.harnessd.instance.lock`). Findings SWCAS-01/SWCAS-02.

User-decided fork (2026-06-10): option (b) — a SEPARATE persistent single-instance lock file,
DISTINCT from the per-mutation EX lock (`.harnessd.lock`). The daemon acquires it non-blocking
(LOCK_EX|LOCK_NB) at boot and HOLDS the open handle for the process lifetime; a second instance
refuses to start LOUDLY (``DaemonAlreadyRunning``) BEFORE writing anything. The separate file is
structurally required: flock conflicts across fds even within ONE process (verified on darwin), so
a lifetime hold of the §4.3 mutation-lock file would deadlock every executor mutation — the exact
DAEMON §2.3-vs-§4.3 one-file conflict this fix dissolves.

This file is the F-fix pattern of a NEW dedicated test file (cf. test_replay_to_state.py,
test_collapse_result.py): tests/test_daemon.py stays FROZEN/byte-stable.

BIAS TO REAL (Lesson 7): REAL daemon.boot -> REAL genesis -> REAL chokepoint/executor/on-disk
ledger under a tmp RUNTIME_ROOT; REAL fcntl.flock probes on FRESH fds prove the hold BY FACT (no
monkeypatched lock). The only fakes are the RuntimeAdapter (dry-run SpawnResult, no real pane) and
tmux.list_targets — the established test_daemon.py pair.

LOAD-BEARING (each test kills a named mutant):
  * boot HOLDS the instance lock after returning (mutant: a scoped acquire-and-release — the old
    genesis STEP1+2 pattern — lets a fresh flock succeed)
  * a second daemon REFUSES to start and writes NOTHING (mutant: acquire after write_runtime_json
    -> runtime.json clobbered by the loser; mutant: swallow the BlockingIOError -> no raise)
  * the instance lock is DISTINCT from the mutation lock (mutant: INSTANCE_LOCK_FILENAME =
    '.harnessd.lock' -> the §2.3-vs-§4.3 deadlock: the per-mutation flock probe blocks and every
    executor mutation hangs)
  * boot twice on the SAME root is idempotent, not self-refusing (mutant: a naive unconditional
    re-acquire — flock self-conflicts across fds, so the daemon would refuse against ITSELF)
"""

from __future__ import annotations

import fcntl
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import harnessd.config as config
import harnessd.executor as executor
import harnessd.genesis as genesis
import harnessd.ledger as ledger
from harnessd.spawn import chokepoint


def _daemon():
    return importlib.import_module("harnessd.daemon")


L1_ADDRESS = "L1#exec"


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_daemon.py exactly (the established daemon-boot pair),
# plus the instance-lock finalizer: the daemon's lifetime hold lives in a module
# global, so each test releases it on exit (no held-fd leak across tmp roots).
# ---------------------------------------------------------------------------

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


@pytest.fixture(autouse=True)
def _release_instance_lock():
    """Release the daemon's lifetime instance-lock hold after every test (module-global fd)."""
    yield
    daemon = _daemon()
    release = getattr(daemon, "release_instance_lock", None)
    if callable(release):
        release()


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
            session_uuid="sess-instance-lock-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L1"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path="/runtime/transcripts/sess-instance-lock-0001.jsonl",
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
    """Assemble the ``runtime`` descriptor boot(runtime) consumes (the test_daemon.py shape)."""
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


def _flock_nb_conflicts(path: Path) -> bool:
    """True iff a FRESH fd cannot take LOCK_EX|LOCK_NB on ``path`` — i.e. someone holds it BY FACT.

    Same-process probing is valid on darwin/Linux: flock conflicts between two distinct open file
    descriptions even within one process (verified on darwin — the linchpin of this whole fix).
    """
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _boot_runtime(runtime_root, tmux=None):
    fake = FakeAdapter()
    _install_adapter(fake)
    return _runtime_descriptor(runtime_root, fake_adapter=fake, tmux=tmux or FakeTmux({}))


# ===========================================================================
# boot acquires AND HOLDS the instance lock for the process lifetime (§2.3).
# ===========================================================================

def test_boot_acquires_and_HOLDS_the_instance_lock(runtime):
    daemon = _daemon()
    rt = _boot_runtime(runtime)

    daemon.boot(rt)

    lock_file = runtime / ".harnessd.instance.lock"
    assert lock_file.is_file(), (
        "daemon.boot must create + acquire the persistent single-instance lock at "
        "<runtime_root>/.harnessd.instance.lock (DAEMON §2.3, the two-lock model)"
    )
    # The hold must be BY FACT after boot RETURNS: a fresh fd's LOCK_EX|LOCK_NB must conflict.
    # Mutant killed: a scoped acquire-and-release (the old genesis STEP1+2 pattern) releases the
    # flock before boot returns -> the fresh flock SUCCEEDS -> this assert fails.
    assert _flock_nb_conflicts(lock_file), (
        "the instance lock must still be HELD after boot returns (a lifetime hold, not a scoped "
        "acquire-and-release): a fresh LOCK_EX|LOCK_NB on it must raise BlockingIOError"
    )


# ===========================================================================
# A second daemon REFUSES to start — loudly, and BEFORE writing anything (§2.3).
# ===========================================================================

def test_second_daemon_refuses_to_start_and_writes_nothing(runtime):
    daemon = _daemon()
    rt = _boot_runtime(runtime)

    # Simulate the live FIRST daemon: a REAL flock held on the instance-lock path by our own
    # fresh fd (no monkeypatch — flock conflicts across fds even in one process).
    lock_file = runtime / ".harnessd.instance.lock"
    holder = lock_file.open("a+", encoding="utf-8")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

        with pytest.raises(daemon.DaemonAlreadyRunning) as excinfo:
            daemon.boot(rt)

        # The §2.3 refusal message, verbatim spirit: "another harnessd instance already holds the lock".
        assert "another harnessd instance" in str(excinfo.value), (
            "the refusal must be LOUD and name the cause (DAEMON §2.3: 'another harnessd instance "
            "already holds the lock')"
        )
        # The loser must clobber NOTHING: the refusal precedes the runtime.json descriptor write.
        # Mutant killed: acquiring the instance lock AFTER write_runtime_json -> runtime.json appears.
        assert not (runtime / "runtime.json").exists(), (
            "a refused second daemon must write NOTHING — the instance-lock acquire must precede "
            "the runtime.json descriptor write (no clobber of the live daemon's descriptor)"
        )
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


# ===========================================================================
# The instance lock is DISTINCT from the per-mutation lock — the resolved
# §2.3-vs-§4.3 conflict: a lifetime hold must NOT block executor mutations.
# ===========================================================================

def test_instance_lock_is_distinct_from_the_mutation_lock(runtime):
    daemon = _daemon()
    rt = _boot_runtime(runtime)

    daemon.boot(rt)

    # (1) Two files: the lifetime instance guard and the §4.3 per-mutation EX domain never share
    # a path. Mutant killed: INSTANCE_LOCK_FILENAME = '.harnessd.lock' (the unresolved one-file
    # conflict) -> (2) below conflicts and (3) deadlocks.
    instance_path = genesis.instance_lock_path(runtime)
    mutation_path = executor.lock_path()
    assert Path(instance_path) != Path(mutation_path), (
        "the instance lock must be a SEPARATE file from the per-mutation .harnessd.lock — a "
        "lifetime hold of the mutation lock would deadlock every executor mutation (flock "
        "conflicts across fds even in one process; DAEMON §2.3-vs-§4.3 resolution)"
    )

    # (2) The per-mutation domain is NOT blocked by the lifetime hold: a fresh EX|NB flock on
    # .harnessd.lock succeeds (and is released) while the instance lock is held.
    assert not _flock_nb_conflicts(Path(mutation_path)), (
        "while the instance lock is held, the per-mutation .harnessd.lock must remain FREE "
        "between mutations — the two serialization domains must not contend"
    )

    # (3) A REAL executor mutation commits WHILE the instance lock is held: the booted L1 binding
    # transitions running->blocked through the one writer, and the post-state lands on disk.
    live = ledger.read_binding(L1_ADDRESS)
    assert live is not None and live.get("state") == "running", (
        "precondition: boot must have spawned the L1 root to running (the test_daemon.py arc)"
    )
    result = executor.transition(
        L1_ADDRESS,
        expected_state=live["state"],
        expected_generation=live["generation"],
        expected_owner_token=None,  # daemon-internal mutation — the EX lock serializes (§4.3)
        target_state="blocked",
        binding_delta={"state": "blocked"},
        event="state_transition",
        summary="F6: a real mutation must commit while the instance lock is held",
    )
    assert result.ok, (
        f"executor.transition must commit while the instance lock is held (errors={result.errors}) "
        "— a shared lock file would flock-deadlock here"
    )
    assert ledger.read_binding(L1_ADDRESS).get("state") == "blocked", (
        "the mutation's post-state must be on disk — committed THROUGH the per-mutation lock, "
        "concurrent with the lifetime instance hold"
    )


# ===========================================================================
# boot twice on the SAME root in one process: idempotent, NOT self-refusing.
# ===========================================================================

def test_boot_twice_same_root_is_idempotent_not_self_refusing(runtime):
    daemon = _daemon()
    rt = _boot_runtime(runtime)

    daemon.boot(rt)
    # The in-process relaunch/test-reuse case: the SAME process re-boots the SAME root. flock
    # self-conflicts across fds, so a naive unconditional re-acquire would refuse against ITSELF.
    # Mutant killed: acquire_instance_lock without the same-path-held no-op guard.
    daemon.boot(rt)  # must NOT raise DaemonAlreadyRunning

    # And the hold survives the second boot: the lock is still held by fact.
    lock_file = runtime / ".harnessd.instance.lock"
    assert _flock_nb_conflicts(lock_file), (
        "after the idempotent second boot the instance lock must STILL be held (the no-op path "
        "must not release the live hold)"
    )


# ===========================================================================
# F14 fold-in (the F6-deferred descriptor-schema change, commit 355feae): the §2.3
# self-report names lock_path — and it names the INSTANCE lock, not the per-mutation
# domain. Both write_runtime_json call sites (daemon.boot + genesis STEP 2) pass it.
# ===========================================================================

def test_boot_stamps_lock_path_into_runtime_json(runtime):
    daemon = _daemon()
    rt = _boot_runtime(runtime)

    daemon.boot(rt)

    data = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
    expected = str(genesis.instance_lock_path(runtime))
    assert data.get("lock_path") == expected, (
        "runtime.json must carry lock_path naming the INSTANCE lock (DAEMON §2.3 self-report: "
        "'lock_path names the INSTANCE lock') — the descriptor-schema change F6 deferred into "
        f"F14; got {data.get('lock_path')!r}, expected {expected!r}"
    )
