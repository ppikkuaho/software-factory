"""Increment 13 — load-bearing STRENGTHENING (mutation-review gate).

Gap: the headline property "the CLI is NOT a writer" was not robustly load-bearing. The coverage
review defeated BOTH guards with one mutant: guard 1 (source-grep) listed executor.* but OMITTED
chokepoint.collapse/resume/release_claim + ledger.append_wal; guard 2 (cli-alone-cannot-mutate) only
exercised the `transition` command. So a CLI-side `chokepoint.collapse` in the `kill` path evaded both.

Robust fix: the BEHAVIORAL guard (no daemon reachable -> ledger byte-for-byte unchanged) run over EVERY
mutation command (transition / kill / spawn). If the CLI cannot mutate with no daemon for ANY command,
it is genuinely not a writer — this catches a CLI-side write via ANY primitive, not just the grepped ones.
Plus: complete guard 1's writer list.
"""

import inspect
import json

import pytest

import harnessd.fencing as fencing
import harnessd.harnessctl as harnessctl
import harnessd.ledger as ledger


NODE = "proj/widget#exec"


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


def _seed(state="running", generation=4):
    token = fencing.mint_owner_token(NODE, "sa", "uuid", 2)
    rec = {"node_address": NODE, "parent_address": "proj#exec", "level": "L5", "subagent_id": "sa",
           "session_uuid": "uuid", "state": state, "generation": generation, "lease_epoch": 2,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "working", "tmux_target": "harness:x"}
    ledger.write_binding({NODE: rec}, _lock_held=True)
    return rec, token


def _run_dead_socket(argv, dead_socket, capsys, monkeypatch):
    """Run the CLI against a socket with NO listener; return (raised, code)."""
    main = harnessctl.main
    params = inspect.signature(main).parameters
    kwargs, full = {}, list(argv)
    if "socket_path" in params:
        kwargs["socket_path"] = str(dead_socket)
    else:
        full = ["--socket", str(dead_socket)] + full
        monkeypatch.setenv("HARNESSD_SOCKET", str(dead_socket))
    try:
        code = main(full, **kwargs)
        return False, code
    except (ConnectionError, FileNotFoundError, OSError):
        return True, None


# --- Guard 1 completed: the writer-name list must include the chokepoint mutators + append_wal ----

def test_harnessctl_names_no_writer_including_chokepoint_collapse():
    source = inspect.getsource(harnessctl)
    for writer in ("write_binding", "append_wal", "executor.transition", "executor.claim",
                   "executor.collapse", "executor.release_lease", "executor.heartbeat",
                   "executor.watchdog_checkpoint", "claim_and_spawn", "chokepoint.collapse",
                   "chokepoint.resume", "release_claim", ".collapse(", ".transition(", ".claim("):
        assert writer not in source, (
            f"the harnessctl CLIENT must NOT call {writer!r} — every mutation routes through the daemon "
            "(DAEMON §4.3: CLIs are clients, not writers)"
        )


# --- THE robust behavioral guard: no daemon -> NO mutation, for EVERY mutation command ------------

@pytest.mark.parametrize("argv", [
    ["transition", NODE, "--target-state", "blocked", "--event", "block"],
    ["kill", NODE],
    ["spawn", NODE, "--level", "L5"],
])
def test_cli_alone_cannot_mutate_any_command(runtime, tmp_path, capsys, monkeypatch, argv):
    """With NO daemon reachable, EVERY mutation command must leave the ledger BYTE-FOR-BYTE unchanged.
    A CLI-side write via ANY primitive (executor.* OR chokepoint.collapse in kill, etc.) is caught here."""
    _seed(state="running", generation=4)
    before = _read_map()
    dead = tmp_path / "no-daemon.sock"
    raised, code = _run_dead_socket(argv, dead, capsys, monkeypatch)
    assert raised or (code is not None and code != 0), \
        f"{argv[0]!r} with no daemon must FAIL (the CLI alone cannot mutate)"
    assert _read_map() == before, (
        f"THE load-bearing property: {argv[0]!r} with no daemon must leave the ledger UNCHANGED — "
        "the CLI is not a writer (a CLI-side write via any primitive would change it -> caught)"
    )


def _read_map():
    return ledger.all_nodes()
