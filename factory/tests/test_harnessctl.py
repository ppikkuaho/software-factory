"""Increment 13 — FROZEN acceptance for harnessctl (the CLI client) + the daemon IPC handler.
Tests ONLY — NO implementation. RED first.

The OPERATOR interface. The cardinal rule (DAEMON §4.3): the CLI is a CLIENT, NEVER a writer — it
sends a request to the resident daemon over a local socket, and the DAEMON performs the mutation
inside the ONE EX serialization lock. Read-only commands may take the shared lock directly.

Authoritative sources (grounded, not recalled):
  * IMPLEMENTATION-PLAN §3 module table (harnessctl.py row, L65): "CLI client — NOT a writer. Sends
    requests to the resident daemon over a local socket/FIFO; the daemon performs the mutation inside
    the one lock. Read-only commands (show/next/validate/reconcile-inspect) may take the shared lock
    directly." GENERALIZE: build_parser L1613-1696 subcommand structure -> node-addressed.
  * IMPLEMENTATION-PLAN §2.x / Increment-13 Done-test (L800-803): "Node-addressed subcommands
    (spawn/transition/show/reconcile-inspect/kill) over a local socket/FIFO to the daemon; read-only
    commands may take the shared lock directly. Done-test: a mutation via harnessctl is performed by
    the daemon inside the one lock (not by the CLI process); a read command returns ledger state."
  * DAEMON §4.3 (Lock discipline — one serialization domain): "CLIs are clients, not writers. A
    harnessctl command sends a request to the resident daemon (over a local socket/fifo), and the
    daemon performs the mutation inside the one lock. No external process ever read-modify-replaces
    binding-ledger.yaml directly." §4.5: read-only (shared lock) = show/next/validate/reconcile-inspect;
    mutating (exclusive lock) = transition / claim / collapse-kill.

BIAS TO REAL (Lesson 7):
  * a REAL local unix-domain socket for the IPC round-trip (no in-memory shortcut);
  * the REAL executor + REAL on-disk ledger (tmp RUNTIME_ROOT) for the mutation;
  * the REAL reconcile for the inspect command;
  * the daemon-side IPC HANDLER is REAL — it performs the mutation THROUGH THE EXECUTOR inside
    store.file_lock(EX) (the single writer).
  Only the spawn ADAPTER is faked (a dry-run RuntimeAdapter, consistent with Inc 10/12).

The handler is driven via a real socket round-trip, kept BOUNDED: a single accept/handle on a handler
thread that is joined and torn down in a fixture. NEVER an unbounded serve-forever loop in a test path.

NO IMPLEMENTATION here — harnessd/harnessctl.py and the daemon IPC handler do not exist yet (RED until
written).

LOAD-BEARING (each test names the mutant it kills):
  * the CLI is NOT a writer: a mutation lands via the DAEMON handler inside the lock, and the harnessctl
    CLIENT module never calls ledger.write_binding / executor.transition (mutant: have the CLI write the
    ledger directly -> caught by the source/attr assertion AND by the cli-alone-cannot-mutate property);
  * cli-alone-cannot-mutate: with NO handler reachable, harnessctl alone does NOT change the ledger
    (mutant: the CLI mutates directly -> the ledger changes with no daemon -> caught);
  * a read command (show <addr>) returns the REAL ledger state for that node (mutant: return a
    stale/empty value -> caught);
  * a mutation request actually changes the ledger via the executor (mutant: handler no-ops -> the
    binding is unchanged -> caught);
  * node addresses parse + route: the addressed node is the one mutated/read (mutant: drop the address
    arg -> the wrong/absent node is touched -> caught).
"""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import harnessd.config as config
import harnessd.addressing as addressing
import harnessd.executor as executor  # the REAL single writer the handler routes the mutation through
import harnessd.fencing as fencing
import harnessd.ledger as ledger
from harnessd.spawn import chokepoint


# ---------------------------------------------------------------------------
# Module probes — harnessctl is the CLIENT; the IPC handler lives in harnessd/ipc.py
# OR on harnessd/daemon.py (the §3 module table leaves the handler's home to the builder:
# "in daemon.py or a harnessd/ipc.py"). We probe both, defensively, like test_daemon.py.
# ---------------------------------------------------------------------------

def _harnessctl():
    return importlib.import_module("harnessd.harnessctl")


def _ipc():
    """The daemon-side IPC handler module/home. Prefer harnessd.ipc; fall back to harnessd.daemon."""
    for name in ("harnessd.ipc", "harnessd.daemon"):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if _resolve_handle(mod) is not None and _resolve_serve_one(mod) is not None:
            return mod
    # Default to harnessd.ipc so the RED failure names the expected handler home.
    return importlib.import_module("harnessd.ipc")


def _resolve_handle(mod):
    """The request->response handler: handle_request(request: dict) -> dict (the lock-held mutator)."""
    for attr in ("handle_request", "handle", "dispatch", "handle_message"):
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


def _resolve_serve_one(mod):
    """The BOUNDED server primitive: a single accept/handle (NEVER serve-forever)."""
    for attr in ("serve_one", "accept_one", "handle_one", "serve_once"):
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


def _subcommand_choices(parser) -> set:
    """The set of registered subcommand names on an argparse parser (the subparsers' choices)."""
    choices: set = set()
    for action in parser._actions:
        if getattr(action, "choices", None):
            try:
                choices.update(action.choices.keys())
            except AttributeError:
                pass
    return choices


# ---------------------------------------------------------------------------
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so EVERY pathless ledger
# call (read_binding / write_binding / append_wal) AND the executor's EX lock land
# under the test tree. Restores the prior value so tests don't leak runtime state.
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


# ---------------------------------------------------------------------------
# Fake spawn adapter (the ONLY fake — consistent with Inc 10/12). The mutation path
# itself (executor + on-disk ledger + the EX lock) is REAL.
# ---------------------------------------------------------------------------

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
            session_uuid="sess-harnessctl-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L3"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path="/runtime/transcripts/sess-harnessctl-0001.jsonl",
            failure_class=None,
        )


def _install_adapter(fake):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
    else:
        chokepoint.ADAPTER = fake


# ---------------------------------------------------------------------------
# Direct binding seed — the suite-wide path (ledger.write_binding(map, _lock_held=True)).
# ---------------------------------------------------------------------------

NODE = "proj/widget#exec"
SUBAGENT = "subagent-aaaa1111"
SESSION = "sess-uuid-0001"


def _seed_binding(*, state="running", generation=4, lease_epoch=2, node=NODE):
    """Seed ONE live binding directly through the ledger and return (binding, owner_token).

    ADDITIVE: reads the current whole map first and splices this node in, so seeding a second node
    does NOT clobber the first (write_binding is a WHOLE-MAP atomic-replace, §2.4/§4.3 — seeding two
    nodes one at a time with a bare {node: binding} map would drop the bystander)."""
    owner_token = fencing.mint_owner_token(node, SUBAGENT, SESSION, lease_epoch)
    binding = {
        "node_address": node,
        "parent_address": "proj#exec",
        "level": "L3",
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": 0,
        "liveness_state": "working",
    }
    whole_map = ledger.all_nodes()
    whole_map[node] = copy.deepcopy(binding)
    ledger.write_binding(whole_map, _lock_held=True)
    return binding, owner_token


def _read(node=NODE):
    return ledger.read_binding(node)


# ---------------------------------------------------------------------------
# A BOUNDED, real-socket daemon IPC handler harness. One AF_UNIX listener; a handler
# thread that accepts EXACTLY ONE connection, hands the framed request to the REAL
# handler (which mutates through the executor inside the EX lock), writes the JSON
# response back, and EXITS. Joined + closed in the fixture teardown — NEVER serve-forever.
# ---------------------------------------------------------------------------

def _recv_all(conn) -> bytes:
    chunks = []
    while True:
        data = conn.recv(65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


class BoundedDaemonIPC:
    """A real AF_UNIX socket bound to a bounded single-accept handler thread.

    The handler thread runs the REAL daemon IPC handler ONCE per accepted connection for a fixed,
    finite number of connections (default 1), then exits. There is no serve-forever loop: the thread
    body is a bounded for-range over the expected connection count, and the fixture joins it.
    """

    def __init__(self, socket_path: Path, *, expected_connections: int = 1):
        self.socket_path = Path(socket_path)
        self.expected = expected_connections
        self.handled: list[dict] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.socket_path))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._run, name="bounded-ipc", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        handle = _resolve_handle(_ipc())
        for _ in range(self.expected):  # BOUNDED — not serve-forever
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return
            with conn:
                raw = _recv_all(conn)
                request = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                self.handled.append(request)
                # The REAL handler performs the mutation through the executor inside the EX lock.
                response = handle(request)
                conn.sendall(json.dumps(response).encode("utf-8"))

    def close(self):
        # Tear down deterministically: unblock + join the bounded thread, close the listener,
        # remove the socket file. Never leaves a serve-forever thread or a stray socket behind.
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture
def ipc_server(runtime, tmp_path):
    """Stand up the bounded real-socket IPC handler; tear it down (join + unlink) afterward."""
    socket_path = tmp_path / "harnessd.sock"
    server = BoundedDaemonIPC(socket_path, expected_connections=1).start()
    try:
        yield server
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Invoke harnessctl as the CLIENT. The client serializes a request, sends it over the
# local socket, and prints the JSON response + sets an exit code. We thread the socket
# path through whatever channel the client exposes (a kwarg, then a --socket flag, then
# an env override) so the frozen test does not over-constrain the wiring detail.
# ---------------------------------------------------------------------------

def _run_cli(argv, *, socket_path=None, capsys=None, monkeypatch=None):
    """Run harnessctl.main(argv) and return (exit_code, parsed_json_stdout_or_None).

    socket_path is threaded to the client by the FIRST channel main() accepts:
      1. main(argv, socket_path=...)            (a keyword the client exposes), else
      2. an argv flag --socket <path>           (prepended), else
      3. the HARNESSD_SOCKET env var            (monkeypatched).
    """
    harnessctl = _harnessctl()
    main = harnessctl.main

    full_argv = list(argv)
    kwargs = {}
    if socket_path is not None:
        params = inspect.signature(main).parameters
        if "socket_path" in params:
            kwargs["socket_path"] = str(socket_path)
        elif "sock" in params:
            kwargs["sock"] = str(socket_path)
        else:
            # Fall back to a --socket flag, then an env override.
            full_argv = ["--socket", str(socket_path)] + full_argv
            if monkeypatch is not None:
                monkeypatch.setenv("HARNESSD_SOCKET", str(socket_path))

    code = main(full_argv, **kwargs)

    payload = None
    if capsys is not None:
        out = capsys.readouterr().out
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    return code, payload


# ===========================================================================
# 1. PARSE + ROUTE — the node-addressed argparse CLI exposes the §2.x subcommands.
#    Mutant killed: a missing subcommand / dropped address arg -> parse fails / wrong route.
# ===========================================================================

def test_build_parser_exposes_node_addressed_subcommands(runtime):
    harnessctl = _harnessctl()
    assert hasattr(harnessctl, "build_parser"), (
        "harnessctl must expose build_parser() (the §3 GENERALIZE of the recovered "
        "build_parser L1613-1696 subcommand structure -> node-addressed)"
    )
    parser = harnessctl.build_parser()

    # Every subcommand the Increment-13 Done-test names (L800-803) must be a registered choice:
    # the reads (show / reconcile-inspect) AND the mutations (spawn / transition / kill).
    choices = _subcommand_choices(parser)
    for cmd in ("show", "reconcile-inspect", "spawn", "transition", "kill"):
        assert cmd in choices, (
            f"build_parser must register the {cmd!r} subcommand (Increment-13 Done-test L800-803: "
            "node-addressed spawn/transition/show/reconcile-inspect/kill)"
        )

    # A node-addressed READ command CARRIES the address positional (drop-the-address-arg mutant).
    # 'show' is the minimal read with no other required flags, so we can parse it directly + route it.
    args = parser.parse_args(["show", NODE])
    assert getattr(args, "command", None) == "show", "build_parser must ROUTE the show subcommand"
    addr = getattr(args, "addr", None) or getattr(args, "address", None) or getattr(args, "node", None)
    assert addr == NODE, (
        "a node-addressed subcommand (show <addr>) must parse the address positional "
        "(mutant: drop the address arg -> the wrong/absent node is addressed)"
    )


def test_parser_has_the_read_only_command_surface(runtime):
    """The §4.5 read-only surface (show / next-seq / validate / reconcile-inspect) is present."""
    harnessctl = _harnessctl()
    parser = harnessctl.build_parser()
    # next-seq / validate are read-only commands (§4.5 'next <node>' / 'validate'). The exact spelling
    # is the builder's, but at least one next-seq-flavored and one validate-flavored command must exist.
    choices = _subcommand_choices(parser)
    assert "show" in choices, "the read command 'show' must exist (§4.5 read-only surface)"
    assert "reconcile-inspect" in choices, (
        "the read command 'reconcile-inspect' must exist (§4.5 / Done-test L800-803)"
    )
    assert any("next" in c for c in choices), "a next-seq read command must exist (§4.5)"
    assert any("validate" in c for c in choices), "a validate read command must exist (§4.5)"


def test_parser_exposes_run_scoped_operator_helpers(runtime):
    choices = _subcommand_choices(_harnessctl().build_parser())
    assert {"start", "status", "attach"} <= choices
    assert "genesis" not in choices


def test_parser_serializes_the_fenced_intent_revision_channel(runtime):
    harnessctl = _harnessctl()
    args = harnessctl.build_parser().parse_args(
        [
            "intent-revise",
            "L1#exec",
            "--target",
            "L1/widget#exec",
            "--candidate-ref",
            "intent-revision-candidate.md",
            "--reason",
            "Client confirmed the amendment.",
            "--expected-owner-token",
            "owner-token",
        ]
    )
    request = harnessctl._build_request(args)
    assert request == {
        "command": "intent-revise",
        "addr": "L1#exec",
        "target_address": "L1/widget#exec",
        "candidate_ref": "intent-revision-candidate.md",
        "reason": "Client confirmed the amendment.",
        "expected_owner_token": "owner-token",
    }


# ===========================================================================
# 2. THE CLI IS NOT A WRITER — the harnessctl CLIENT module has NO direct write path.
#    It does socket I/O + arg parsing ONLY; it never imports/calls ledger.write_binding
#    or executor.transition. Mutant killed: have the CLI write the ledger directly -> caught.
# ===========================================================================

def test_harnessctl_module_has_no_direct_write_path():
    harnessctl = _harnessctl()
    source = inspect.getsource(harnessctl)

    # The CLIENT must not name the single-writer primitives at all (it routes through the daemon).
    assert "write_binding" not in source, (
        "the harnessctl CLIENT must NOT call ledger.write_binding — it is a client, not a writer "
        "(DAEMON §4.3: 'CLIs are clients, not writers'). Route the mutation through the daemon handler."
    )
    assert "executor.transition" not in source and ".transition(" not in source, (
        "the harnessctl CLIENT must NOT call executor.transition — the daemon performs the mutation "
        "inside the one lock (§4.3). The CLI only serializes a request + does socket I/O."
    )
    # And it must not call the chokepoint mutators directly either (those, too, are daemon-side).
    for writer in ("claim_and_spawn", "executor.claim", "executor.heartbeat", "executor.watchdog_checkpoint"):
        assert writer not in source, (
            f"the harnessctl CLIENT must NOT call {writer} directly — every mutation routes through "
            "the daemon IPC handler (§4.3)."
        )


def test_harnessctl_does_socket_io_not_ledger_io():
    """The client's mutation path is socket I/O, not ledger I/O — it imports socket, not the writer."""
    harnessctl = _harnessctl()
    source = inspect.getsource(harnessctl)
    assert "socket" in source, (
        "the harnessctl CLIENT must speak to the daemon over a local socket (DAEMON §4.3 / Done-test "
        "L800-803: 'over a local socket/FIFO to the daemon')"
    )


# ===========================================================================
# 3. cli-alone-cannot-mutate — with NO daemon handler reachable, harnessctl alone
#    does NOT change the ledger. THE load-bearing property: the CLI cannot mutate by
#    itself; the mutation is the DAEMON's. Mutant killed: the CLI mutates directly ->
#    the ledger changes even with no daemon -> caught.
# ===========================================================================

def test_cli_alone_cannot_mutate_when_handler_unreachable(runtime, tmp_path, capsys, monkeypatch):
    binding, _token = _seed_binding(state="running", generation=4)
    before = _read()

    # Point the client at a socket path with NO listener bound (the daemon is absent/unreachable).
    dead_socket = tmp_path / "no-daemon.sock"
    assert not dead_socket.exists()

    # A transition mutation request to an absent daemon: the CLI cannot perform it itself. It either
    # returns a non-zero exit code (connection refused / no daemon) OR raises a connection error — in
    # NEITHER case may the on-disk ledger change (the CLI is not a writer).
    raised = False
    code = None
    try:
        code, _payload = _run_cli(
            ["transition", NODE, "--target-state", "blocked", "--event", "block"],
            socket_path=dead_socket,
            capsys=capsys,
            monkeypatch=monkeypatch,
        )
    except (ConnectionError, FileNotFoundError, OSError):
        raised = True

    assert raised or (code is not None and code != 0), (
        "with NO daemon reachable, the transition command must FAIL (nonzero exit or a connection "
        "error) — the CLI alone cannot perform the mutation (DAEMON §4.3: CLIs are clients, not writers)"
    )
    assert _read() == before, (
        "THE load-bearing property: with no daemon handler reachable the ledger must be BYTE-FOR-BYTE "
        "UNCHANGED — the CLI is not a writer (mutant: the CLI mutates directly -> the ledger changes "
        "with no daemon -> caught)"
    )


# ===========================================================================
# 4. THE DONE-TEST: a MUTATION via harnessctl is performed by the DAEMON inside the
#    ONE lock — the ledger reflects the mutation AFTER the real socket round-trip, and
#    the mutation went through the REAL executor (the daemon handler is the writer).
#    Mutant killed: handler no-ops -> the binding is unchanged -> caught.
# ===========================================================================

def test_mutation_via_harnessctl_is_performed_by_the_daemon(ipc_server, capsys, monkeypatch):
    binding, token = _seed_binding(state="running", generation=4)
    assert _read()["state"] == "running"

    # A real round-trip: harnessctl serializes a `transition` request, sends it over the real unix
    # socket to the bounded handler, which performs the running->blocked transition THROUGH THE REAL
    # executor inside store.file_lock(EX). running->blocked is a legal edge (§states ALLOWED_TRANSITIONS).
    code, payload = _run_cli(
        [
            "transition", NODE,
            "--expected-state", "running",
            "--expected-generation", "4",
            "--expected-owner-token", token,
            "--target-state", "blocked",
            "--event", "block",
        ],
        socket_path=ipc_server.socket_path,
        capsys=capsys,
        monkeypatch=monkeypatch,
    )

    # The handler actually received a request over the real socket (the round-trip happened).
    assert ipc_server.handled, (
        "the daemon IPC handler must have received the request over the real socket "
        "(Done-test L800-803: the mutation is performed BY THE DAEMON, not the CLI process)"
    )

    # The ledger reflects the mutation AFTER the round-trip: the binding advanced running->blocked,
    # its per-node generation bumped (the executor committed it), exactly once.
    after = _read()
    assert after is not None and after["state"] == "blocked", (
        "after the round-trip the ledger must reflect the mutation (running->blocked) performed by the "
        "daemon through the REAL executor (mutant: handler no-ops -> binding unchanged -> caught)"
    )
    assert after["generation"] == binding["generation"] + 1, (
        "the executor's CAS-guarded commit must have bumped the per-node generation exactly once "
        "(proves the mutation went through the REAL single writer, not a raw poke)"
    )
    # And the run-ledger WAL carries the committing event (the executor's intent-first append).
    wal = ledger.load_wal()
    assert any(r.get("node_address") == NODE and r.get("to_state") == "blocked" for r in wal), (
        "the daemon's executor commit must have appended the running->blocked WAL row (intent-first)"
    )


# ===========================================================================
# 5. A READ command (show <addr>) returns the REAL ledger state for that node.
#    Mutant killed: return a stale/empty value -> caught.
# ===========================================================================

def test_show_returns_real_ledger_state(ipc_server, capsys, monkeypatch):
    binding, _token = _seed_binding(state="blocked", generation=7, lease_epoch=3)

    code, payload = _run_cli(
        ["show", NODE],
        socket_path=ipc_server.socket_path,
        capsys=capsys,
        monkeypatch=monkeypatch,
    )

    assert code == 0, "a successful read (show) must exit 0 (print-JSON/exit-code convention)"
    assert payload is not None, "show must print the node's ledger state as JSON to stdout"

    # The read returns the REAL ledger state for THAT node — not a stale/empty value.
    flat = json.dumps(payload)
    assert NODE in flat, "show <addr> must return the state for the ADDRESSED node (parse+route)"
    assert "blocked" in flat and "7" in flat, (
        "show must return the REAL current ledger state (state='blocked', generation=7) — "
        "mutant: return a stale/empty value -> caught"
    )


def test_show_of_absent_node_is_not_a_phantom(ipc_server, capsys, monkeypatch):
    """A read of an UNSEEDED address returns absent (None/empty), never a fabricated binding.

    Mutant killed: a read that returns a phantom/canned binding for any address -> caught (the real
    ledger has no such node, so the read must report it absent)."""
    _seed_binding(state="running", generation=4)  # seed a DIFFERENT node only
    absent = "proj/ghost#exec"

    code, payload = _run_cli(
        ["show", absent],
        socket_path=ipc_server.socket_path,
        capsys=capsys,
        monkeypatch=monkeypatch,
    )

    # Absent node: the read must NOT report it as the seeded node's state. Either an explicit
    # null/empty payload or a non-zero exit code — never the wrong node's binding.
    flat = json.dumps(payload) if payload is not None else ""
    assert NODE not in flat, (
        "a read of an absent address must NOT leak a DIFFERENT node's binding (parse+route is real)"
    )
    assert payload in (None, {}, [], "null") or code != 0 or "null" in flat.lower() or absent in flat, (
        "a read of an absent node must report it absent (null/empty/nonzero) — not a phantom binding"
    )


# ===========================================================================
# 6. THE DAEMON HANDLER IS THE WRITER — handle_request performs the mutation through
#    the REAL executor inside the EX lock (driven DIRECTLY here, no socket, to pin the
#    handler-as-writer contract independently of the transport).
#    Mutant killed: the handler does not route through the executor -> no commit -> caught.
# ===========================================================================

def test_handler_performs_mutation_through_the_executor(runtime):
    ipc = _ipc()
    handle = _resolve_handle(ipc)
    assert handle is not None, (
        "the daemon IPC handler must expose a request handler (handle_request(request)->response) — "
        "the ONLY writer path for a CLI-originated mutation (§4.3)"
    )

    binding, token = _seed_binding(state="running", generation=4)

    request = {
        "command": "transition",
        "addr": NODE,
        "expected_state": "running",
        "expected_generation": 4,
        "expected_owner_token": token,
        "target_state": "blocked",
        "event": "block",
    }
    response = handle(request)

    after = _read()
    assert after["state"] == "blocked" and after["generation"] == 5, (
        "the daemon handler must perform the mutation THROUGH THE EXECUTOR inside the lock "
        "(running->blocked, generation 4->5) — mutant: handler no-ops -> binding unchanged -> caught"
    )
    # The response reports success (the print-JSON/exit-code convention the client prints + exits on).
    assert isinstance(response, dict), "the handler must return a JSON-serializable response dict"
    assert response.get("ok", True) is not False, (
        "a committed mutation must report ok (the client prints the response + sets exit 0)"
    )


def test_handler_routes_addressed_node_only(runtime):
    """The handler mutates the ADDRESSED node and ONLY that node (parse+route is real end-to-end).

    Mutant killed: the handler ignores the address and mutates a fixed/other node -> the bystander
    changes -> caught."""
    ipc = _ipc()
    handle = _resolve_handle(ipc)

    target, target_token = _seed_binding(state="running", generation=4, node=NODE)
    other = "proj/bystander#exec"
    other_binding, _other_token = _seed_binding(state="running", generation=2, lease_epoch=5, node=other)
    other_before = _read(other)

    handle({
        "command": "transition",
        "addr": NODE,
        "expected_state": "running",
        "expected_generation": 4,
        "expected_owner_token": target_token,
        "target_state": "blocked",
        "event": "block",
    })

    assert _read(NODE)["state"] == "blocked", "the ADDRESSED node must be the one mutated"
    assert _read(other) == other_before, (
        "the BYSTANDER node must be byte-for-byte unchanged — the handler routes to the addressed "
        "node only (mutant: ignore the address / mutate a fixed node -> the bystander changes -> caught)"
    )


# ===========================================================================
# 7. KILL (a MUTATION) routes through the daemon too — kill <addr> collapses the node
#    to a terminal state via the daemon handler (which routes through chokepoint.collapse
#    -> the REAL executor). Exercised over the real socket round-trip.
#    Mutant killed: kill no-ops / the CLI kills directly -> caught.
# ===========================================================================

def test_kill_via_harnessctl_collapses_through_the_daemon(ipc_server, capsys, monkeypatch):
    _install_adapter(FakeAdapter())  # spawn-adapter present (consistency with Inc 10/12), unused by kill
    binding, token = _seed_binding(state="running", generation=4)

    code, payload = _run_cli(
        [
            "kill", NODE,
            "--expected-owner-token", token,
            "--terminal-signal", "FAILED",
        ],
        socket_path=ipc_server.socket_path,
        capsys=capsys,
        monkeypatch=monkeypatch,
    )

    assert ipc_server.handled, "the kill mutation must round-trip through the daemon over the socket"
    after = _read()
    assert after is not None and after["state"] in ("failed", "dead"), (
        "kill <addr> must collapse the node to a terminal state via the daemon handler "
        "(running --FAILED--> failed), routed through the REAL executor — mutant: kill no-ops -> caught"
    )


# ===========================================================================
# 8. THE BOUNDED-HANDLER DISCIPLINE — the IPC handler exposes a BOUNDED single-accept
#    primitive (serve_one / accept_one), NOT only an unbounded serve-forever loop. We
#    assert the bounded primitive EXISTS and DO NOT drive any unbounded loop.
# ===========================================================================

def test_ipc_exposes_a_bounded_single_accept_primitive(runtime):
    ipc = _ipc()
    serve_one = _resolve_serve_one(ipc)
    assert serve_one is not None, (
        "the daemon IPC must expose a BOUNDED single-accept primitive (serve_one/accept_one) so a "
        "test can drive exactly one accept/handle — NEVER only an unbounded serve-forever loop "
        "(mirrors daemon.poll_once: the loop body must be drivable in a single bounded step)"
    )
    # We assert only its SHAPE — we never call an unbounded serve loop in a test path.
    sig = inspect.signature(serve_one)
    assert len(sig.parameters) >= 1, (
        "the bounded accept primitive must take the socket/listener it accepts on (a single bounded "
        "accept/handle), not an implicit global serve-forever"
    )


# ===========================================================================
# RUN-SCOPED OPERATOR HELPERS — start is the sole pre-daemon lifecycle exception;
# status/attach still obtain node truth through daemon IPC.
# ===========================================================================


def test_start_dispatches_before_socket_resolution_and_can_skip_attach(
    tmp_path, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    monkeypatch.setenv(
        harnessctl.commissioning.FIDELITY_PLAYBACK_AUTHORITY_ENV,
        "operator-delegate",
    )
    monkeypatch.setenv(
        harnessctl.commissioning.FIDELITY_PLAYBACK_DELEGATE_ENV,
        "synthetic-owner",
    )
    monkeypatch.setenv(
        harnessctl.commissioning.FIDELITY_PLAYBACK_DELEGATION_REASON_ENV,
        "owner-ruled synthetic trace",
    )
    spec = SimpleNamespace(
        runtime_root=(tmp_path / "run").resolve(),
        build_id="build-operator",
        label="com.harness.daemon.test",
        request_id="request-1",
        paths=SimpleNamespace(
            socket=tmp_path / "run" / ".harnessd" / "harnessd.sock",
            stdout_log=tmp_path / "run" / ".harnessd" / "daemon.out",
            stderr_log=tmp_path / "run" / ".harnessd" / "daemon.err",
        ),
    )
    calls = []
    monkeypatch.setattr(
        harnessctl.commissioning,
        "resolve_runtime_identity",
        lambda **_kwargs: (spec.runtime_root, spec.build_id),
    )
    monkeypatch.setattr(
        harnessctl.lifecycle,
        "prepare_run",
        lambda **kwargs: calls.append(("prepare", kwargs)) or spec,
    )
    monkeypatch.setattr(
        harnessctl.lifecycle,
        "bootstrap_run",
        lambda passed: calls.append(("bootstrap", passed)),
    )
    monkeypatch.setattr(
        harnessctl,
        "_wait_for_l1",
        lambda _spec, *, timeout_s: calls.append(("wait", timeout_s))
        or {"node_address": "L1#exec", "state": "running", "tmux_target": "harness-L1:0.0"},
    )
    monkeypatch.setattr(
        harnessctl,
        "_round_trip",
        lambda *_args, **_kwargs: pytest.fail(
            "start must not resolve the ordinary socket path first"
        ),
    )

    code = harnessctl.main(
        [
            "start",
            "--build-id",
            "build-operator",
            "--runtime-root",
            str(spec.runtime_root),
            "--intake",
            "Build it.",
            "--wait-seconds",
            "2",
            "--no-attach",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert calls[0][0] == "prepare"
    assert calls[0][1]["initial_intake"] == "Build it."
    assert calls[0][1]["fidelity_playback_authority"] == "operator-delegate"
    assert calls[0][1]["fidelity_playback_delegate"] == "synthetic-owner"
    assert (
        calls[0][1]["fidelity_playback_delegation_reason"]
        == "owner-ruled synthetic trace"
    )
    assert calls[1] == ("bootstrap", spec)
    assert calls[2] == ("wait", 2.0)


def test_start_parses_two_scoped_and_broad_module_arms_in_one_invocation(
    tmp_path, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    spec = SimpleNamespace(
        runtime_root=(tmp_path / "run").resolve(),
        build_id="mixed-panel-run",
        label="com.harness.daemon.test",
        request_id="request-1",
        paths=SimpleNamespace(
            socket=tmp_path / "run" / ".harnessd" / "harnessd.sock",
            stdout_log=tmp_path / "run" / ".harnessd" / "daemon.out",
            stderr_log=tmp_path / "run" / ".harnessd" / "daemon.err",
        ),
    )
    captured = []
    monkeypatch.setattr(
        harnessctl.commissioning,
        "resolve_runtime_identity",
        lambda **_kwargs: (spec.runtime_root, spec.build_id),
    )
    monkeypatch.setattr(
        harnessctl.lifecycle,
        "prepare_run",
        lambda **kwargs: captured.append(kwargs) or spec,
    )
    monkeypatch.setattr(harnessctl.lifecycle, "bootstrap_run", lambda _spec: None)
    monkeypatch.setattr(
        harnessctl,
        "_wait_for_l1",
        lambda *_args, **_kwargs: {
            "node_address": "L1#exec",
            "state": "running",
            "tmux_target": "harness-L1:0.0",
        },
    )

    code = harnessctl.main(
        [
            "start",
            "--build-id",
            "mixed-panel-run",
            "--review-panel-arm",
            "forge-queue/queue-core#exec=composition-interface,risk-readiness",
            "--review-panel-arm",
            "forge-queue/persistence#exec=broad",
            "--no-attach",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert captured[0]["review_panel_arms"] == [
        {
            "pattern": "forge-queue/queue-core#exec",
            "axes": ["composition-interface", "risk-readiness"],
        },
        {
            "pattern": "forge-queue/persistence#exec",
            "axes": ["broad"],
        },
    ]


@pytest.mark.parametrize(
    "declaration",
    [
        "forge-queue/queue-core#exec",
        "forge-queue/queue-core#exec=",
        "forge-queue/queue-core#exec=unknown-axis",
        "forge-queue/queue-core#exec=broad,broad",
    ],
)
def test_start_refuses_invalid_panel_arm_before_lifecycle(
    declaration, tmp_path, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    monkeypatch.setattr(
        harnessctl.lifecycle,
        "prepare_run",
        lambda **_kwargs: pytest.fail(
            "invalid panel arm must refuse before lifecycle preparation"
        ),
    )

    code = harnessctl.main(
        [
            "start",
            "--runtime-root",
            str(tmp_path / "run"),
            "--review-panel-arm",
            declaration,
            "--no-attach",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["error_code"] == "review_panel_arm_invalid"
    assert "accepted axes" in payload["errors"][0]


def test_start_refuses_partial_delegate_declaration_before_lifecycle(
    tmp_path, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    monkeypatch.setenv(
        harnessctl.commissioning.FIDELITY_PLAYBACK_AUTHORITY_ENV,
        "operator-delegate",
    )
    monkeypatch.delenv(
        harnessctl.commissioning.FIDELITY_PLAYBACK_DELEGATE_ENV,
        raising=False,
    )
    monkeypatch.delenv(
        harnessctl.commissioning.FIDELITY_PLAYBACK_DELEGATION_REASON_ENV,
        raising=False,
    )
    monkeypatch.setattr(
        harnessctl.lifecycle,
        "prepare_run",
        lambda **_kwargs: pytest.fail(
            "invalid delegation must refuse before lifecycle artifacts"
        ),
    )

    code = harnessctl.main(
        [
            "start",
            "--runtime-root",
            str(tmp_path / "run"),
            "--no-attach",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["ok"] is False
    assert payload["error_code"] == "fidelity_playback_declaration_invalid"
    assert "requires both" in payload["errors"][0]


def test_start_input_error_is_structured_and_creates_no_lifecycle(tmp_path, capsys, monkeypatch):
    harnessctl = _harnessctl()
    monkeypatch.setattr(
        harnessctl.lifecycle,
        "prepare_run",
        lambda **_kwargs: pytest.fail("unreadable intake must fail before lifecycle preparation"),
    )
    code = harnessctl.main(
        ["start", "--intake-file", str(tmp_path / "missing.md"), "--no-attach"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["ok"] is False
    assert "intake" in payload["errors"][0]


def test_start_surfaces_typed_readiness_evidence(tmp_path, capsys, monkeypatch):
    harnessctl = _harnessctl()
    spec = SimpleNamespace(
        runtime_root=(tmp_path / "run").resolve(),
        build_id="build-operator",
        label="com.harness.daemon.test",
        request_id="request-1",
        paths=SimpleNamespace(
            socket=tmp_path / "run" / ".harnessd" / "harnessd.sock",
            stdout_log=tmp_path / "run" / ".harnessd" / "daemon.out",
            stderr_log=tmp_path / "run" / ".harnessd" / "daemon.err",
        ),
    )
    monkeypatch.setattr(
        harnessctl.commissioning,
        "resolve_runtime_identity",
        lambda **_kwargs: (spec.runtime_root, spec.build_id),
    )
    monkeypatch.setattr(harnessctl.lifecycle, "prepare_run", lambda **_kwargs: spec)
    monkeypatch.setattr(harnessctl.lifecycle, "bootstrap_run", lambda _spec: None)
    monkeypatch.setattr(
        harnessctl,
        "_wait_for_l1",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            harnessctl.lifecycle.LifecycleReadinessError(
                "daemon crashed before readiness",
                error_code="daemon_crashed",
                details={
                    "service_state": "waiting",
                    "process_alive": False,
                    "restart_count": 3,
                    "socket_present": False,
                    "stderr_tail": ["Traceback:", "FileNotFoundError: tmux"],
                },
            )
        ),
    )

    code = harnessctl.main(["start", "--build-id", "build-operator", "--no-attach"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["command"] == "start"
    assert payload["error_code"] == "daemon_crashed"
    assert payload["startup"]["restart_count"] == 3
    assert payload["startup"]["stderr_tail"][-1] == "FileNotFoundError: tmux"


def test_status_reuses_tree_ipc_and_never_reads_ledger_locally(
    runtime, tmp_path, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    socket_path = tmp_path / "status.sock"
    server = BoundedDaemonIPC(socket_path, expected_connections=1).start()
    try:
        _seed_binding(state="running", generation=4)
        code = harnessctl.main(["--socket", str(socket_path), "status"])
    finally:
        server.close()
    out = capsys.readouterr().out
    assert code == 0
    assert server.handled == [{"command": "tree"}]
    assert NODE in out


def test_attach_resolves_target_through_show_then_invokes_commissioned_tmux(
    tmp_path, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    calls = []
    monkeypatch.setattr(
        harnessctl,
        "_round_trip",
        lambda socket_path, request: calls.append(("ipc", socket_path, request))
        or {
            "ok": True,
            "binding": {
                "node_address": "L1#exec",
                "state": "running",
                "tmux_target": "harness-L1_exec:0.0",
            },
        },
    )
    monkeypatch.setattr(
        harnessctl.commissioning,
        "resolve_runtime_identity",
        lambda **_kwargs: ((tmp_path / "run").resolve(), "build-operator"),
    )
    monkeypatch.setattr(
        harnessctl.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(("tmux", argv, kwargs))
        or SimpleNamespace(returncode=0),
    )
    code = harnessctl.main(
        ["--socket", "/tmp/harness.sock", "attach", "--build-id", "build-operator"]
    )
    assert code == 0
    assert calls[0] == (
        "ipc",
        "/tmp/harness.sock",
        {"command": "show", "addr": "L1#exec"},
    )
    tmux_call = calls[1]
    assert tmux_call[1] == [
        "tmux",
        "-L",
        "harnessd-build-operator",
        "attach-session",
        "-t",
        "harness-L1_exec:0.0",
    ]
    assert "TMUX" not in tmux_call[2]["env"]


def test_attach_print_only_returns_exact_command_without_starting_tmux(
    tmp_path, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    monkeypatch.setattr(
        harnessctl,
        "_round_trip",
        lambda *_args, **_kwargs: {
            "ok": True,
            "binding": {"tmux_target": "harness-L1_exec:0.0", "state": "running"},
        },
    )
    monkeypatch.setattr(
        harnessctl.commissioning,
        "resolve_runtime_identity",
        lambda **_kwargs: ((tmp_path / "run").resolve(), "b"),
    )
    monkeypatch.setattr(
        harnessctl.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("print-only attach must not invoke tmux"),
    )
    code = harnessctl.main(
        ["--socket", "/tmp/h.sock", "attach", "--build-id", "b", "--print-only"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["command"][-2:] == ["-t", "harness-L1_exec:0.0"]


def test_view_and_journey_are_offline_read_verbs_not_socket_requests(
    runtime, capsys, monkeypatch
):
    harnessctl = _harnessctl()
    _seed_binding()
    node = addressing.node_dir(NODE, runtime)
    node.mkdir(parents=True, exist_ok=True)
    (node / "report.md").write_text("# report\n", encoding="utf-8")
    monkeypatch.setattr(
        harnessctl,
        "_round_trip",
        lambda *_args, **_kwargs: pytest.fail(
            "live-or-dead observability must not require daemon IPC"
        ),
    )

    parser = harnessctl.build_parser()
    view_args = parser.parse_args(
        ["view", "--runtime-root", str(runtime), "--format", "json"]
    )
    journey_args = parser.parse_args(
        ["journey", "--runtime-root", str(runtime), "--stdout"]
    )
    assert view_args.command == "view" and view_args.format == "json"
    assert journey_args.command == "journey" and journey_args.stdout is True

    code = harnessctl.main(
        ["view", "--runtime-root", str(runtime), "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["nodes"][0]["node_address"] == NODE

    code = harnessctl.main(
        ["view", "--runtime-root", str(runtime), "--format", "terminal"]
    )
    terminal = capsys.readouterr().out
    assert code == 0 and NODE in terminal

    code = harnessctl.main(
        ["journey", "--runtime-root", str(runtime), "--stdout"]
    )
    html = capsys.readouterr().out
    assert code == 0 and html.startswith("<!doctype html>")
    assert not (runtime / ".harnessd" / "views").exists()


def test_journey_default_output_and_node_tree_refusal_are_structured(
    runtime, capsys
):
    harnessctl = _harnessctl()
    _seed_binding()
    node = addressing.node_dir(NODE, runtime)
    node.mkdir(parents=True, exist_ok=True)
    (node / "report.md").write_text("# report\n", encoding="utf-8")

    code = harnessctl.main(["journey", "--runtime-root", str(runtime)])
    payload = json.loads(capsys.readouterr().out)
    expected = runtime / ".harnessd" / "views" / "journey.html"
    assert code == 0 and payload["ok"] is True
    assert Path(payload["output"]) == expected
    assert expected.is_file()

    forbidden = node / "journey.html"
    code = harnessctl.main(
        [
            "journey",
            "--runtime-root",
            str(runtime),
            "--output",
            str(forbidden),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["ok"] is False
    assert "node tree" in payload["errors"][0]
    assert not forbidden.exists()


def test_view_reports_malformed_binding_without_traceback(runtime, capsys):
    harnessctl = _harnessctl()
    (runtime / ledger.BINDING_FILENAME).write_text("[]\n", encoding="utf-8")

    code = harnessctl.main(
        ["view", "--runtime-root", str(runtime), "--format", "json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["ok"] is False
    assert "JSON object" in payload["errors"][0]
