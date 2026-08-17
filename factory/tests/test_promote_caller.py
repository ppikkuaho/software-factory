"""F8 — the delivery-terminus CALLER (CRIT-3): the IPC ``promote`` verb + the harnessctl subcommand.

The unit/acceptance tests (test_promote.py / test_increment17_strengthen.py) pin promote() itself.
THESE pin the operational seam a live run actually fires — INTAKE-TO-DELIVERY Stage 5→6: L1
owner-final CONFIRM becomes a deliberate ``harnessctl promote <addr> --decision accept`` from L1's pane, serialized to the
resident daemon's IPC ``promote`` verb; the DAEMON performs the cross-jail copy/push and the
executor.deliver stamp (the CLI stays a client, the executor stays the single writer, promotion stays
a harnessd action). Mirrors tests/test_outbox_ipc.py (IPC route + CLI serialization) plus ONE bounded
real-socket round-trip (the BoundedDaemonIPC pattern from tests/test_harnessctl.py).

LOAD-BEARING (each test names the mutant it kills):
  * 'promote' is a registered _DISPATCH verb (mutant: the terminus stays caller-less — CRIT-3 reborn);
  * an accept decision delivers REAL bytes end-to-end through handle_request -> promote ->
    executor.deliver (mutant: any break in the dispatch->op->single-writer chain);
  * an OMITTED decision holds the gate (mutant: the handler defaults to accept — a fat-fingered
    request must never speculatively cross the jail boundary);
  * an explicit reject is a recorded no-op, /runtime/ intact (mutant: gate-on-presence);
  * a failed PromoteResult is ROUTED into the response (mutant: the IPC layer swallows the failure);
  * the CLI exposes the subcommand with --decision REQUIRED and never imports the promote op
    (the §4.3 cardinal rule: a client, never a writer);
  * the promoted tree carries NO control-plane dotfiles (F19 seeds .sign-off/.signal/.inbox into the
    same node dir the copy-out sources — harness machinery must not ship in the deliverable).

BIAS TO REAL: real accepted-project bindings via ledger.write_binding(_lock_held=True); REAL
deliverable trees at addressing.node_dir(NODE, runtime_root); a real temp-dir destination OUTSIDE the
/runtime/ jail root; a real AF_UNIX socket for the round-trip (bounded single-accept, joined in
teardown — never serve-forever).
"""

from __future__ import annotations

import copy
import inspect
import json
import socket
import threading
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.fencing as fencing
import harnessd.fidelity_playback as fidelity_playback
import harnessd.harnessctl as harnessctl
import harnessd.ipc as ipc
import harnessd.ledger as ledger


NODE = "proj/demo-widget#exec"


@pytest.fixture
def runtime(tmp_path):
    # The /runtime/ jail root is a DISTINCT subdir of tmp_path (test_promote.py precedent): the
    # delivery destination (a sibling under tmp_path) must be genuinely OUTSIDE the jail root for
    # the cross-jail assertions to mean anything.
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = runtime_root
    try:
        yield runtime_root
    finally:
        ledger.RUNTIME_ROOT = previous


# ---------------------------------------------------------------------------
# Real artifacts: the deliverable tree at the CANONICAL nodes/<path>/ workspace
# (addressing.node_dir — the dir every agent actually writes), seeded with the F19
# control-plane dotfiles the daemon/agent exchange there (they must NOT ship).
# ---------------------------------------------------------------------------

DELIVERABLE_FILES = {
    "README.md": "# Demo Widget\n\nThe finished, accepted deliverable.\n",
    "src/widget.py": "def widget():\n    return 'CALLER-DELIVERABLE-MARKER-f8c1'\n",
}

CONTROL_PLANE_DOTFILES = {
    ".sign-off.exec.json": '{"owner_token": "tok-f19", "node_address": "%s"}\n' % NODE,
    ".signal.exec.json": '{"signal": "DONE", "owner_token": "tok-f19"}\n',
    ".inbox.exec.jsonl": '{"from": "parent", "type": "wake"}\n',
}

INTENT_SPEC = """# Intent Spec

## Outcomes
| Outcome ID | Outcome |
|---|---|
| O-001 | The recipient can run the demo widget. |

## Requirements
| ID | Requirement | Tag | MNF | Reflect-back status |
|---|---|---|---|---|
| R-001 | Render the widget | decided | YES | confirmed |
"""

FIDELITY_JUDGMENT = """# Fidelity Judgment

Preliminary Verdict: accept

## Outcome Playback
| Outcome ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| O-001 | Opened the widget | Widget rendered | README.md | accept |

## MNF Playback
| MNF ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| R-001 | Exercised invalid input | Safe refusal | README.md | accept |
"""


def _build_tree(runtime_root, node=NODE):
    """The REAL nodes/<path>/ deliverable workspace + the F19 control-plane dotfiles inside it."""
    d = addressing.node_dir(node, runtime_root)
    for rel, content in DELIVERABLE_FILES.items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    brief = d / "client-brief"
    brief.mkdir(parents=True, exist_ok=True)
    (brief / "fidelity-judgment.md").write_text(
        FIDELITY_JUDGMENT,
        encoding="utf-8",
    )
    (brief / "intent-spec.md").write_text(INTENT_SPEC, encoding="utf-8")
    for name, content in CONTROL_PLANE_DOTFILES.items():
        (d / name).write_text(content, encoding="utf-8")
    return d


def _seed(*, node_address=NODE, delivery_destination, delivery_kind="filesystem-path",
          deliverable_state="completed"):
    """Seed a REAL accepted-project binding (state=done, deliverable block set) through the ledger."""
    token = fencing.mint_owner_token(node_address, "sa-caller", "sess-caller", 2)
    rec = {
        "node_address": node_address, "parent_address": "root#exec", "level": "L1",
        "subagent_id": "sa-caller", "session_uuid": "sess-caller", "state": "done",
        "generation": 5, "lease_epoch": 2, "owner_token": token, "last_applied_seq": 0,
        "liveness_state": "terminal", "gate_crossed_at": None, "paused_at": None,
        "transcript_path": None,
        "deliverable_state": deliverable_state,
        "write_targets": ["proj/demo-widget/"],
        "acceptance_ref": "client-brief/intent-spec.md",
        "delivery_destination": str(delivery_destination), "delivery_kind": delivery_kind,
    }
    ledger.write_binding({node_address: copy.deepcopy(rec)}, _lock_held=True)
    posted = fidelity_playback.create_question(node_address)
    if posted.get("ok"):
        fidelity_playback.answer_question(
            node_address,
            question_id=posted["question_id"],
            decision="confirm",
            note="Owner confirms this exact caller playback.",
        )
    return rec


def _tree_snapshot(root: Path) -> dict:
    snapshot = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _wal_rows(node_address):
    return [r for r in ledger.load_wal() if r.get("node_address") == node_address]


# --------------------------------------------------------------------------- #
# IPC route (direct ipc.handle_request — the daemon-resident handler).
# --------------------------------------------------------------------------- #

def test_promote_is_a_known_ipc_command():
    assert "promote" in ipc._DISPATCH


def test_ipc_promote_accept_delivers_real_bytes(runtime, tmp_path):
    _build_tree(runtime)
    dest = tmp_path / "delivery-out" / "demo-widget"
    _seed(delivery_destination=dest)

    resp = ipc.handle_request({
        "command": "promote", "addr": NODE, "decision": "accept",
        "acceptance_ref": "client-brief/intent-spec.md",
    })

    # The response routes the full PromoteResult.
    assert resp["ok"] is True and resp["command"] == "promote" and resp["addr"] == NODE
    assert resp["delivered"] is True
    assert resp["deliverable_state"] == "delivered"
    assert resp["delivery_destination"] == str(dest)
    assert resp["errors"] == []

    # The marker bytes landed at the OUT-OF-JAIL destination (the daemon's cross-jail write).
    for rel, content in DELIVERABLE_FILES.items():
        landed = dest / rel
        assert landed.is_file(), f"deliverable file {rel!r} did not land at {dest}"
        assert landed.read_text(encoding="utf-8") == content
    assert "CALLER-DELIVERABLE-MARKER-f8c1" in (dest / "src/widget.py").read_text("utf-8")

    # The ledger shows the single-writer stamp: delivered + advanced watermark.
    after = ledger.read_binding(NODE)
    assert after["deliverable_state"] == "delivered"
    assert after["last_applied_seq"] > 0, (
        "the deliver stamp must go through the single writer (executor.deliver advances "
        "last_applied_seq); a raw write would not move the watermark"
    )

    # Every delivery WAL row is the control plane's.
    rows = [r for r in _wal_rows(NODE) if "deliver" in (r.get("event") or "")]
    assert rows and all(r.get("actor") == "harnessd" for r in rows)


def test_ipc_promote_excludes_control_plane_dotfiles(runtime, tmp_path):
    """The F19↔F8 interaction + INT-3/TM-4: ALL harness machinery in the node workspace —
    .sign-off/.signal/.inbox (F19), .harness-outbox (the spawn-request channel + its consumed
    markers), .sandbox-profiles (the rendered-.sb fallback), .*.tmp (atomic_replace residue) —
    must not ship in the deliverable, INCLUDING from NESTED child node dirs (node dirs nest by
    path, so a coordinator's deliverable contains every descendant's machinery surface)."""
    d = _build_tree(runtime)
    # INT-3: the spawn-request outbox (real OUTBOX_DIRNAME) + consumed markers in the node dir.
    from harnessd.spawn.outbox import OUTBOX_DIRNAME

    outbox = d / OUTBOX_DIRNAME
    outbox.mkdir()
    (outbox / "req-001.json").write_text('{"child": "sub"}\n', encoding="utf-8")
    (outbox / "req-001.json.done").write_text("consumed\n", encoding="utf-8")
    # INT-3: the sandbox-profile fallback dir + an atomic_replace tmp residue.
    profiles = d / ".sandbox-profiles"
    profiles.mkdir()
    (profiles / "harness-jail.sb").write_text("(version 1)\n", encoding="utf-8")
    (d / ".brief.md.tmp").write_text("torn atomic-replace residue\n", encoding="utf-8")
    # TM-4: a NESTED CHILD node dir inside the coordinator's deliverable subtree, carrying its
    # OWN full machinery set (node dirs nest by path — the child's dir sits UNDER the parent's).
    child_dir = addressing.node_dir(NODE.split("#", 1)[0] + "/sub#exec", runtime)
    child_dir.mkdir(parents=True, exist_ok=True)
    (child_dir / "result.md").write_text("# child deliverable\n", encoding="utf-8")
    (child_dir / ".sign-off.exec.json").write_text('{"owner_token": "tok-child"}\n', encoding="utf-8")
    (child_dir / ".signal.exec.json").write_text('{"signal": "DONE"}\n', encoding="utf-8")
    (child_dir / ".inbox.exec.jsonl").write_text('{"type": "wake"}\n', encoding="utf-8")
    child_outbox = child_dir / OUTBOX_DIRNAME
    child_outbox.mkdir()
    (child_outbox / "req-002.json.rejected").write_text("spawn error\n", encoding="utf-8")

    dest = tmp_path / "delivery-out" / "demo-widget"
    _seed(delivery_destination=dest)

    resp = ipc.handle_request({"command": "promote", "addr": NODE, "decision": "accept"})
    assert resp["ok"] is True and (dest / "README.md").is_file()
    assert (dest / "sub" / "result.md").is_file(), (
        "the nested child's REAL deliverable bytes must still ship (the exclusion is machinery-"
        "scoped, not a child-dir blackout)"
    )

    shipped = [str(p.relative_to(dest)) for p in dest.rglob("*")]
    for pattern in (".sign-off.", ".signal.", ".inbox."):
        offenders = [f for f in shipped if Path(f).name.startswith(pattern)]
        assert not offenders, (
            f"control-plane dotfiles shipped in the deliverable: {offenders} — the F19 "
            "sign-off/signal/inbox machinery must be excluded from the promoted tree "
            "(NESTED child dirs included, TM-4)"
        )
    for basename in (OUTBOX_DIRNAME, ".sandbox-profiles"):
        offenders = [f for f in shipped if basename in Path(f).parts]
        assert not offenders, (
            f"harness machinery {basename!r} shipped in the deliverable: {offenders} (INT-3 — "
            "spawn-request JSONs / consumed markers / sandbox profiles are control plane, not product)"
        )
    tmp_offenders = [f for f in shipped
                     if Path(f).name.startswith(".") and Path(f).name.endswith(".tmp")]
    assert not tmp_offenders, (
        f"atomic-replace .*.tmp residue shipped in the deliverable: {tmp_offenders} (INT-3)"
    )


def test_ipc_promote_without_decision_holds_the_gate(runtime, tmp_path):
    """An OMITTED decision ships accept_signal=None — the gate HOLDS. Kills the
    handler-defaults-to-accept mutant (a bare request must never speculatively deliver)."""
    _build_tree(runtime)
    dest = tmp_path / "delivery-out" / "demo-widget"
    _seed(delivery_destination=dest)

    resp = ipc.handle_request({"command": "promote", "addr": NODE})  # NO decision field

    assert resp["ok"] is False
    assert resp["errors"], "a held gate must surface a structured reason, never a silent no-op"
    assert not dest.exists(), (
        "a decision-less promote request wrote to the destination — the handler default-accepted"
    )
    assert ledger.read_binding(NODE)["deliverable_state"] == "completed", (
        "deliverable_state changed on a held gate"
    )


def test_ipc_promote_reject_is_noop_runtime_intact(runtime, tmp_path):
    source = _build_tree(runtime)
    dest = tmp_path / "delivery-out" / "demo-widget"
    _seed(delivery_destination=dest)
    before = _tree_snapshot(source)

    resp = ipc.handle_request({"command": "promote", "addr": NODE, "decision": "reject"})

    assert resp["ok"] is False
    assert not dest.exists(), "a REJECT decision wrote to the destination — the gate is broken"
    assert _tree_snapshot(source) == before, "/runtime/ source tree mutated on a reject no-op"
    assert ledger.read_binding(NODE)["deliverable_state"] != "delivered"


def test_ipc_promote_failure_is_surfaced_not_swallowed(runtime, tmp_path):
    """A genuine OS failure (destination parent is a regular file) — the failed PromoteResult is
    ROUTED into the response AND journaled, never swallowed into an ok."""
    _build_tree(runtime)
    blocker = tmp_path / "blocker-file"
    blocker.write_text("i am a file, not a directory\n", encoding="utf-8")
    dest = blocker / "demo-widget"  # parent is a FILE -> the real copy-out raises
    _seed(delivery_destination=dest)

    resp = ipc.handle_request({"command": "promote", "addr": NODE, "decision": "accept"})

    assert resp["ok"] is False
    assert resp["errors"], "the failure reason must be routed into the response"
    assert resp["deliverable_state"] == "delivery-failed"

    after = ledger.read_binding(NODE)
    assert after["deliverable_state"] == "delivery-failed"
    events = [r.get("event") for r in _wal_rows(NODE)]
    assert "delivery_failed_escalation" in events, (
        f"the distinct §6.3 escalation row is missing — events: {events}"
    )


def test_ipc_promote_retry_destination_override_after_failure(runtime, tmp_path):
    _build_tree(runtime)
    blocker = tmp_path / "blocker-file-retry"
    blocker.write_text("i am a file, not a directory\n", encoding="utf-8")
    stale_dest = blocker / "demo-widget"
    retry_dest = tmp_path / "delivery-out" / "demo-widget"
    _seed(delivery_destination=stale_dest)

    first = ipc.handle_request({"command": "promote", "addr": NODE, "decision": "accept"})
    assert first["ok"] is False

    retry = ipc.handle_request({
        "command": "promote",
        "addr": NODE,
        "decision": "accept",
        "delivery_destination_override": str(retry_dest),
        "delivery_kind_override": "filesystem-path",
    })

    assert retry["ok"] is True and retry["delivered"] is True, retry["errors"]
    assert (retry_dest / "src" / "widget.py").is_file()
    after = ledger.read_binding(NODE)
    assert after["delivery_destination"] == str(retry_dest)
    assert (after.get("delivery_failed_targets") or [])[-1]["destination"] == str(stale_dest)


# --------------------------------------------------------------------------- #
# harnessctl subcommand (the CLIENT — serialization only, never a writer).
# --------------------------------------------------------------------------- #

def test_harnessctl_exposes_promote_subcommand_with_required_decision():
    parser = harnessctl.build_parser()

    args = parser.parse_args(["promote", NODE, "--decision", "accept"])
    assert args.command == "promote" and args.addr == NODE and args.decision == "accept"
    request = harnessctl._build_request(args)
    assert request["command"] == "promote" and request["addr"] == NODE
    assert request["decision"] == "accept"

    # --decision is REQUIRED: argparse refuses a bare `promote <addr>` (no fat-fingered
    # speculative delivery from the CLI surface either).
    with pytest.raises(SystemExit):
        parser.parse_args(["promote", NODE])

    # choices reject anything outside {accept, reject}.
    with pytest.raises(SystemExit):
        parser.parse_args(["promote", NODE, "--decision", "maybe"])

    # The cardinal rule (§4.3): the CLIENT never imports/calls the promote op.
    source = inspect.getsource(harnessctl)
    assert "harnessd.promote" not in source and "promote.promote" not in source, (
        "harnessctl must stay a client — serialization + socket I/O only, never the promote op"
    )


def test_harnessctl_promote_serializes_optional_fields():
    parser = harnessctl.build_parser()
    args = parser.parse_args([
        "promote", NODE, "--decision", "reject",
        "--acceptance-ref", "client-brief/intent-spec.md",
        "--delivery-source", "build/app",
        "--delivery-destination", "/tmp/retry-target",
        "--delivery-kind", "filesystem-path",
        "--note", "scope drift on R-002",
    ])
    request = harnessctl._build_request(args)
    assert request == {
        "command": "promote",
        "addr": NODE,
        "decision": "reject",
        "acceptance_ref": "client-brief/intent-spec.md",
        "delivery_source": "build/app",
        "delivery_destination_override": "/tmp/retry-target",
        "delivery_kind_override": "filesystem-path",
        "note": "scope drift on R-002",
    }


# --------------------------------------------------------------------------- #
# ONE bounded real-socket round-trip: CLI -> socket -> daemon handler -> promote ->
# executor.deliver -> bytes at the destination. The BoundedDaemonIPC pattern from
# tests/test_harnessctl.py — a single accept on a joined thread, never serve-forever.
# --------------------------------------------------------------------------- #

def _recv_all(conn) -> bytes:
    chunks = []
    while True:
        data = conn.recv(65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


class BoundedDaemonIPC:
    """A real AF_UNIX socket served by a BOUNDED single-accept handler thread (joined in close)."""

    def __init__(self, socket_path: Path, *, expected_connections: int = 1):
        self.socket_path = Path(socket_path)
        self.expected = expected_connections
        self.handled: list = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.socket_path))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._run, name="bounded-ipc-promote", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        for _ in range(self.expected):  # BOUNDED — not serve-forever
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return
            with conn:
                raw = _recv_all(conn)
                request = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                self.handled.append(request)
                response = ipc.handle_request(request)  # the REAL daemon-resident handler
                conn.sendall(json.dumps(response).encode("utf-8"))

    def close(self):
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


def test_promote_via_harnessctl_round_trip_delivers(runtime, tmp_path, capsys):
    """Full loop-level wiring on real artifacts: any break in the CLI-serialization -> socket ->
    dispatch -> promote -> single-writer chain is killed here."""
    _build_tree(runtime)
    dest = tmp_path / "delivery-out" / "demo-widget"
    _seed(delivery_destination=dest)

    server = BoundedDaemonIPC(tmp_path / "harnessd.sock", expected_connections=1).start()
    try:
        code = harnessctl.main(
            ["promote", NODE, "--decision", "accept",
             "--acceptance-ref", "client-brief/intent-spec.md"],
            socket_path=str(server.socket_path),
        )
    finally:
        server.close()

    assert code == 0, f"the accept round-trip must exit 0; stdout: {capsys.readouterr().out!r}"

    # The server saw the serialized request (the CLI shipped the right verb + decision).
    assert server.handled and server.handled[0]["command"] == "promote"
    assert server.handled[0]["addr"] == NODE and server.handled[0]["decision"] == "accept"

    # The DAEMON-side handler performed the cross-jail write + the single-writer stamp.
    assert (dest / "src" / "widget.py").is_file(), "deliverable bytes did not land at the destination"
    assert "CALLER-DELIVERABLE-MARKER-f8c1" in (dest / "src" / "widget.py").read_text("utf-8")
    assert ledger.read_binding(NODE)["deliverable_state"] == "delivered"

    # The client printed the daemon's JSON response (the machine surface).
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True and payload["delivered"] is True


def test_ipc_promote_delivery_source_ships_product_surface_only(runtime, tmp_path):
    root = _build_tree(runtime)
    product = root / "build" / "app"
    (product / "logview").mkdir(parents=True)
    (product / "pyproject.toml").write_text("[project]\nname = 'logview'\n", encoding="utf-8")
    (product / "logview" / "__main__.py").write_text("print('product root')\n", encoding="utf-8")
    dest = tmp_path / "delivery-out" / "logview"
    _seed(delivery_destination=dest)

    resp = ipc.handle_request({
        "command": "promote",
        "addr": NODE,
        "decision": "accept",
        "delivery_source": "build/app",
    })

    assert resp["ok"] is True and resp["delivered"] is True, resp["errors"]
    assert (dest / "pyproject.toml").is_file()
    assert (dest / "logview" / "__main__.py").is_file()
    assert not (dest / "build").exists()
    assert not (dest / "client-brief").exists()
    assert ledger.read_binding(NODE)["delivery_source"] == "build/app"
