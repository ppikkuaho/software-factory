"""ipc — the daemon-side IPC request handler: the ONLY writer path for a CLI-originated mutation.

Authoritative sources:
  - IMPLEMENTATION-PLAN §3 module table (harnessctl.py row, L65): "CLI client — NOT a writer. Sends
    requests to the running daemon over a local socket/FIFO; the daemon performs the mutation inside
    the one lock. Read-only commands (show/next/validate/reconcile-inspect) may take the shared lock
    directly."
  - IMPLEMENTATION-PLAN §2.x / Increment-13 Done-test (L800-803): node-addressed subcommands
    (spawn/transition/show/reconcile-inspect/kill) over a local socket/FIFO to the daemon; a mutation
    via harnessctl is performed BY THE DAEMON inside the one lock (not by the CLI process); a read
    command returns ledger state.
  - DAEMON §4.3 (Lock discipline — one serialization domain): "CLIs are clients, not writers." The
    daemon performs the mutation inside the one EX lock. §4.5: read-only (shared lock) =
    show/next/validate/reconcile-inspect; mutating (exclusive lock) = transition / claim / collapse-kill.

THE WRITER SIDE. harnessctl (the CLIENT) NEVER mutates: it serializes a request and ships it over the
local socket. This module is the in-daemon handler that RECEIVES that request and performs the
mutation THROUGH THE EXECUTOR inside ``store.file_lock(EX)`` (the single writer). Every mutation funnels
through ``harnessd.executor`` / ``harnessd.spawn.chokepoint`` — this module never read-modify-replaces
the binding ledger directly. Reads return ledger state (``ledger.read_binding`` / ``ledger.all_nodes`` /
``ledger.next_seq`` / ``reconcile`` for inspect).

BUILDER DECISIONS (the §2.x details the frozen tests leave open — stated in the build report):

  * REQUEST / RESPONSE SCHEMA (FORK-IPC-SCHEMA). A request is a JSON object:
    ``{"command": <name>, "addr": <node-address>, ...command-specific fields}``. A response is a JSON
    object ``{"ok": <bool>, "command": <name>, ...}`` carrying, per command:
      - transition/kill: ``ok``, ``errors`` (the executor's SPECIFIC abort reason — a CAS miss /
        fencing rejection / illegal edge / no-such-node; empty on success), ``warnings``, ``binding``
        (post-state). ``kill`` additionally echoes ``terminal_signal``.
      - promote:         ``ok``, ``addr``, ``delivered``, ``deliverable_state``,
        ``delivery_destination``, ``errors`` — EVERY PromoteResult field routed into the response
        (no result-swallowing). The gate opens ONLY on an explicit ``decision == 'accept'`` field.
      - merge:           ``ok``, ``addr``, ``merged``, ``source_branch``, ``target_branch``,
        ``requested_by``, ``errors`` — a real git merge routed through the gate-owned merge seam.
      - show:            ``ok``, ``addr``, ``binding`` (the node's ledger slice, or ``null`` if absent).
      - next/next-seq:   ``ok``, ``next_seq``.
      - validate:        ``ok``, ``errors``, ``warnings`` (the whole-ledger admission scan).
      - reconcile-inspect: ``ok``, ``adopted``/``necroed``/``escalations`` (a DRY read-only sweep).
    ``ok`` is ``False`` on any abort (a CAS miss, an illegal edge, a fencing rejection, an unknown
    command); the client prints the response + sets a nonzero exit code on ``ok is False``.

  * THE BOUNDED SINGLE-ACCEPT PRIMITIVE (``serve_one``). ``serve_one(listener)`` accepts EXACTLY ONE
    connection, reads the framed request, runs ``handle_request`` (the lock-held mutator/reader), writes
    the JSON response back, and returns. It is the §2.12 ``poll_once`` analogue for the IPC loop: a
    single drivable accept/handle so a test (and the production serve loop) drive it one step at a time.
    There is NO unbounded serve-forever loop in this module's test path — ``serve_forever`` exists for
    production but is NEVER exercised by a test (it simply loops ``serve_one``).

  * RECONCILE-INSPECT IS READ-ONLY (FORK-INSPECT-DRY). The §4.5 ``reconcile-inspect`` read returns the
    divergence verdict WITHOUT mutating: it replays the WAL into an in-memory map and classifies it,
    but does NOT persist or drive the executor. It reports what a reconcile WOULD do (a dry inspect),
    consistent with "read-only commands may take the shared lock directly" (a real reconcile_tick is the
    daemon's poll loop, not a CLI-triggered write).
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
from pathlib import Path
from typing import Optional

from . import (
    addressing,
    clock,
    config,
    detector_signals,
    fidelity_playback,
    ledger,
    merge_gate,
    messages,
    notary,
    plan_alignment_cell,
    store,
    validate,
)
from . import executor as _executor
from . import promote as _promote
from . import reconcile as _reconcile
from .spawn import chokepoint
from .spawn import outbox as _outbox


# ---------------------------------------------------------------------------
# The framed request/response transport (mirrors the WAL's <len>\t framing spirit, but the IPC
# transport is a single connection: we read to EOF, write the whole response, and the peer reads to
# EOF). The test harness sends the request then shuts down its write half; the bounded server reads to
# EOF, handles, and writes the response back on the same connection.
#
# THE READ IS BOUNDED (IPC-DEADLOCK-2026-08-01). EOF framing puts the frame's END in the PEER's hands,
# and this is a SINGLE-THREADED accept loop: a peer that never signals EOF parks the whole control
# plane. That is not hypothetical — on 2026-07-31 one orphaned ``harnessctl message`` client held its
# write half open, the daemon consumed all 10,899 request bytes and then sat in ``recv`` for 14 hours,
# the 64-deep listen backlog filled, and every later client got ECONNREFUSED while the daemon kept
# running and writing WAL. The bound below makes the loop's liveness independent of peer behaviour —
# and therefore independent of WHICH mechanism suppressed that peer's EOF.
# ---------------------------------------------------------------------------

# The per-``recv`` IDLE bound on an accepted connection: how long the daemon will wait for the NEXT
# byte (or EOF) before abandoning the connection. Deliberately generous — the largest observed real
# request (~1.2MB over a local AF_UNIX socket) completes sub-second, so no legitimate client comes
# near it — and deliberately an idle bound rather than a total request deadline: a total deadline
# would have to be sized against the largest legitimate request, an idle bound only against the
# longest legitimate silence, which is the sharper of the two knives against the observed failure.
REQUEST_IDLE_TIMEOUT_S = 30.0


def _harden_connection(conn) -> None:
    """Bound and seal one accepted connection BEFORE any byte is read from it.

    Two properties, both per-connection:
      * the read bound (``REQUEST_IDLE_TIMEOUT_S``) — a socket-level timeout, so it covers the
        response ``sendall`` on the same connection for free (the write leg's OSError guard in
        ``serve_one`` already catches ``TimeoutError``, which IS an ``OSError``);
      * CLOSE-ON-EXEC — a daemon-spawned subprocess must never inherit an IPC connection. A leaked
        DAEMON-side fd keeps the connection half-open from the CLIENT's side: the daemon closes, the
        kernel still counts a live reference in some unrelated child, and the client hangs waiting
        for a response nobody will ever write. It cannot affect the daemon's OWN read — EOF there is
        gated by CLIENT-side references, which no daemon-side flag can influence, and that is
        exactly why fd inheritance was refuted as the 2026-07-31 outage's mechanism. Python makes
        accepted sockets non-inheritable by default (PEP 446), so this is hygiene and a pin, not a
        behaviour change and not the fix for that incident.
    """
    conn.settimeout(REQUEST_IDLE_TIMEOUT_S)
    conn.set_inheritable(False)


def _recv_all(conn: socket.socket) -> bytes:
    """Read a whole request from ``conn`` until the peer closes its write half (EOF-framed).

    Raises ``TimeoutError`` if the peer produces neither a byte nor EOF within the connection's
    ``REQUEST_IDLE_TIMEOUT_S`` bound (set by ``_harden_connection``). The caller turns that into a
    structured abort and RETURNS TO ``accept`` — the daemon never waits on a peer indefinitely.
    """
    chunks: list[bytes] = []
    while True:
        data = conn.recv(65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# handle_request — the request->response dispatcher. The ONLY writer path for a CLI-originated
# mutation: every mutating command routes through the executor / chokepoint (the single writer) inside
# the one EX lock; every read returns ledger state.
# ---------------------------------------------------------------------------


def handle_request(request: dict) -> dict:
    """Dispatch one CLI request to its handler and return a JSON-serializable response dict.

    Mutations (transition / kill / spawn) route THROUGH THE EXECUTOR / CHOKEPOINT (the single writer)
    inside ``store.file_lock(EX)``; this handler never read-modify-replaces the binding ledger itself.
    Reads (show / next / validate / reconcile-inspect) return ledger state.

    A request is ``{"command": <name>, "addr": <addr>, ...}``. An unknown / missing command is a
    structured abort (``ok=False``) — never a silent no-op (the client surfaces it as a nonzero exit).
    """
    if not isinstance(request, dict):
        return {"ok": False, "errors": ["malformed request: expected a JSON object"]}

    command = request.get("command")
    handler = _DISPATCH.get(command)
    if handler is None:
        return {
            "ok": False,
            "command": command,
            "errors": [
                f"unknown command {command!r}: the daemon IPC routes "
                f"{sorted(_DISPATCH)} (Increment-13 node-addressed surface)"
            ],
        }
    return handler(request)


# ---------------------------------------------------------------------------
# Mutating handlers — every one routes through the executor / chokepoint (the single writer).
# ---------------------------------------------------------------------------


def _handle_transition(request: dict) -> dict:
    """A lifecycle transition — routed THROUGH THE EXECUTOR (the single writer) inside the EX lock.

    Carries the CAS preconditions (expected_state / expected_generation / expected_owner_token) the
    client serialized. The executor checks them all BEFORE any write (§4.2) and commits intent-first.
    """
    addr = request.get("addr")
    result = _executor.transition(
        addr,
        expected_state=request.get("expected_state"),
        expected_generation=request.get("expected_generation"),
        expected_owner_token=request.get("expected_owner_token"),
        target_state=request.get("target_state"),
        binding_delta=request.get("binding_delta") or {},
        event=request.get("event", "transition"),
        summary=request.get("summary", "harnessctl transition"),
    )
    return _transition_response("transition", addr, result)


def _handle_message(request: dict) -> dict:
    """Daemon-side convenience authoring of the same canonical agent marker surface."""
    sender = str(request.get("addr") or "")
    target = str(request.get("to") or "")
    message_id = str(request.get("message_id") or "")
    content = request.get("message_content")
    if not content:
        return {
            "ok": False,
            "command": "message",
            "addr": sender,
            "errors": ["message requires content (--text/--file)"],
        }
    try:
        answer_asker = str(request.get("answers_asker") or "").strip()
        answer_message_id = str(request.get("answers_message_id") or "").strip()
        answer_ref = None
        if answer_asker or answer_message_id:
            answer_ref = {
                "asker_address": answer_asker,
                "message_id": answer_message_id,
            }
        result = messages.author_and_submit(
            sender,
            target=target,
            message_id=message_id,
            content=str(content),
            summary=str(request.get("summary") or ""),
            needs_answer=bool(request.get("needs_answer")),
            tags=list(request.get("tags") or []),
            answers_question=answer_ref,
            runtime_root=ledger.RUNTIME_ROOT,
        )
    except Exception as exc:
        return {
            "ok": False,
            "command": "message",
            "addr": sender,
            "errors": [str(exc)],
            "binding": ledger.read_binding(sender),
        }
    return {
        "ok": bool(getattr(result, "ok", False)),
        "command": "message",
        "addr": sender,
        "to": target,
        "message_id": message_id,
        "artifact": str(result.artifact),
        "marker": str(result.marker),
        "errors": list(getattr(result, "errors", []) or []),
        "binding": getattr(result, "binding", None),
    }


def _handle_kill(request: dict) -> dict:
    """A terminal collapse (kill <addr>) — routed THROUGH chokepoint.collapse -> the REAL executor.

    ``kill`` collapses the node to a terminal state (DONE->done, FAILED/DIED*->failed, DEAD->dead).
    chokepoint.collapse is NOT a writer itself: it routes through ``executor.transition`` (the single
    writer) inside the one EX lock. The default signal is FAILED (an operator kill is a failure-class
    teardown, not a clean DONE).

    The TransitionResult is ROUTED into the response (F2r ipc-2, mirroring watchdog.check_leaf's F2b
    routing): an abort surfaces the executor's SPECIFIC structured reason (a CAS miss / fencing
    rejection / illegal edge) in ``errors``, and collapse-returns-None (no binding for the address)
    is an explicit "no such node" abort. The old post-read heuristic (state in the terminal set ->
    ok) phantom-reported success when killing an ALREADY-terminal node even though the transition
    aborted on the illegal edge — no result-swallowing.
    """
    addr = request.get("addr")
    terminal_signal = request.get("terminal_signal", "FAILED")
    expected_owner_token = request.get("expected_owner_token")
    try:
        result = chokepoint.collapse(
            addr,
            terminal_signal,
            expected_owner_token=expected_owner_token,
        )
    except ValueError as exc:
        # collapse REFUSES an asymmetric/unknown signal (e.g. ESCALATED) — surface it, do not crash.
        return {"ok": False, "command": "kill", "addr": addr, "errors": [str(exc)], "binding": None}
    if result is None:
        # collapse found NO binding for the address (nothing to collapse) — name the absence.
        return {
            "ok": False,
            "command": "kill",
            "addr": addr,
            "terminal_signal": terminal_signal,
            "binding": None,
            "errors": [f"kill: no such node {addr!r} — nothing to collapse"],
        }
    response = _transition_response("kill", addr, result)
    response["terminal_signal"] = terminal_signal
    return response


def _handle_spawn(request: dict) -> dict:
    """A spawn (spawn <addr>) — routed THROUGH the chokepoint (the single writer), inside the one lock.

    TWO routes (the ``parent`` field is the discriminator):
      * --parent given -> the PARENT-SPAWNS-CHILD route: ``chokepoint.register_and_spawn_child``
        registers the child UNDER the parent (parent_address SET), writes the brief into the child
        node, then claim-before-spawns it (F-024). The CLI is NOT a writer — the DAEMON performs the
        whole register+brief+spawn here inside the one lock.
      * no --parent -> the EXISTING claim-only spawn of an already-planned node
        (``chokepoint.claim_and_spawn``): STEP1 CAS-claim, STEP3 actor open via the installed adapter.

    The chokepoint's claim is a REAL ``executor.claim`` transition under the one EX lock (the single
    writer); the actor opens through the installed adapter (a dry-run FakeAdapter in tests, the real
    RuntimeAdapter in production). The level config is resolved from the request's ``level`` (defaulting
    to the node's recorded level, else L3).
    """
    addr = request.get("addr")
    live = ledger.read_binding(addr)
    level = request.get("level") or (live.get("level") if live else None) or "L3"
    level_config = config.get_level_config(level)

    parent = request.get("parent") or request.get("parent_address")
    if parent:
        # PARENT-SPAWNS-CHILD: the daemon registers the child under the parent + briefs it + spawns it,
        # all inside the one lock (the CLI shipped only the parent address + the brief_content text).
        result = chokepoint.register_and_spawn_child(
            parent,
            addr,
            child_level_config=level_config,
            brief_content=request.get("brief_content"),
            expected_parent_owner_token=request.get("expected_owner_token"),
        )
    else:
        expected_state = request.get("expected_state")
        expected_generation = request.get("expected_generation")
        if expected_state is None and live is not None:
            expected_state = live.get("state")
        if expected_generation is None and live is not None:
            expected_generation = live.get("generation")
        result = chokepoint.claim_and_spawn(
            addr,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_owner_token=request.get("expected_owner_token"),
            level_config=level_config,
        )
    binding = ledger.read_binding(addr)
    ok = bool(getattr(result, "ok", False))
    return {
        "ok": ok,
        "command": "spawn",
        "addr": addr,
        "parent": parent,
        "session_uuid": getattr(result, "session_uuid", None),
        "model_used": getattr(result, "model_used", None),
        "failure_class": getattr(result, "failure_class", None),
        "binding": binding,
        "errors": [] if ok else [f"spawn failed for {addr!r}: {getattr(result, 'failure_class', None)}"],
    }


def _handle_service_outbox(request: dict) -> dict:
    """Service a node's spawn-request OUTBOX (FORK-SPAWN-CHANNEL) — routed through the chokepoint.

    The agent dropped spawn-requests into its own jail-writable workroot; this drains them. The daemon
    (NOT the agent) composes each child address from the parent's own address and spawns under the
    parent's live owner_token (the parent-fence). With ``addr`` -> service that one node's outbox; with
    no ``addr`` -> service EVERY live non-leaf node (the daemon-loop sweep). Each spawn is a REAL
    register_and_spawn_child through the single writer.
    """
    addr = request.get("addr")
    outcomes = _outbox.service_outbox(addr) if addr else _outbox.service_all_outboxes()
    serviced = [
        {"request": o.request_path, "status": o.status,
         "child_address": o.child_address, "reason": o.reason}
        for o in outcomes
    ]
    spawned = [o for o in serviced if o["status"] == "spawned"]
    return {
        "ok": True,
        "command": "service-outbox",
        "addr": addr,
        "serviced": serviced,
        "spawned_count": len(spawned),
        "rejected_count": len(serviced) - len(spawned),
    }


def _handle_promote(request: dict) -> dict:
    """The F8 delivery-terminus caller (CRIT-3) — routed THROUGH promote() -> executor.deliver.

    L1's deliberate post-confirm delivery trigger (INTAKE-TO-DELIVERY Stage 5→6) arrives as a FLAT explicit
    ``decision`` request field (the ipc style — the _handle_transition precedent); the handler
    synthesizes the node-bound accept signal ITSELF, binding ``node_address=addr`` by
    construction, so a synthesized signal cannot cross-promote another node. An OMITTED decision
    ships ``accept_signal=None`` so promote's gate HOLDS — never default-accept (a bare request
    must never speculatively cross the jail boundary). ``delivery_source`` optionally selects the
    in-jail product surface under the promoted node; promote() fences it to that node workspace.
    ``delivery_destination_override`` is a repair-only retry destination accepted by promote() only
    after a recorded delivery failure.
    promote() performs the cross-jail
    copy/push and stamps the binding via ``executor.deliver`` (the single writer, locks
    internally — the _handle_transition pattern, no extra lock wrapper here). EVERY PromoteResult
    field is routed into the response — the no-result-swallowing rule.
    """
    addr = request.get("addr")
    decision = request.get("decision")
    accept_signal = None if decision is None else {
        "decision": decision,
        "level": "L1",
        "node_address": addr,
        "acceptance_ref": request.get("acceptance_ref"),
        "note": request.get("note"),
    }
    result = _promote.promote(
        addr,
        accept_signal=accept_signal,
        delivery_source=request.get("delivery_source"),
        delivery_destination_override=request.get("delivery_destination_override"),
        delivery_kind_override=request.get("delivery_kind_override"),
    )
    return {
        "ok": bool(result.ok),
        "command": "promote",
        "addr": addr,
        "delivered": result.delivered,
        "deliverable_state": result.deliverable_state,
        "delivery_destination": result.delivery_destination,
        "errors": list(result.errors),
        "playback_authorization": result.playback_authorization,
    }


def _handle_fidelity_playback(request: dict) -> dict:
    """Freeze L1's pointer-only preliminary fidelity question for the human channel."""
    addr = request.get("addr")
    result = fidelity_playback.create_question(addr)
    return {
        "ok": bool(result.get("ok")),
        "command": "fidelity-playback",
        "addr": addr,
        "question_id": result.get("question_id"),
        "question_artifact": result.get("question_artifact"),
        "status": result.get("status"),
        "errors": list(result.get("errors") or []),
        "warnings": list(result.get("warnings") or []),
        "binding": result.get("binding"),
    }


def _handle_merge(request: dict) -> dict:
    """Gate-owned git merge: source node must have cleared review as ``gate_passed``."""
    result = merge_gate.merge_branch(
        request.get("addr"),
        repo_path=request.get("repo_path"),
        target_branch=request.get("target_branch"),
        requested_by=request.get("requested_by"),
    )
    return {
        "ok": bool(result.ok),
        "command": "merge",
        "addr": request.get("addr"),
        "merged": result.merged,
        "source_branch": result.source_branch,
        "target_branch": result.target_branch,
        "requested_by": result.requested_by,
        "errors": list(result.errors),
    }


def _handle_pause(request: dict) -> dict:
    """The F16 pause WRITE verb — routed THROUGH executor.pause (the single writer).

    Sets ``paused_at`` on the addressed node, flagging the whole SUBTREE (node-or-ancestor walk)
    for the two enforcing read-points: chokepoint STEP0 (no new children) and the watchdog's
    §3.4 STEP 0 gate (no recovery actions). NOT a kill — the in-flight agent keeps running. The
    TransitionResult is ROUTED into the response (no result-swallowing).
    """
    addr = request.get("addr")
    result = _executor.pause(addr, expected_owner_token=request.get("expected_owner_token"))
    return _transition_response("pause", addr, result)


def _handle_resume(request: dict) -> dict:
    """The F16 resume WRITE verb — clear ``paused_at`` through executor.resume (the single writer)."""
    addr = request.get("addr")
    result = _executor.resume(addr, expected_owner_token=request.get("expected_owner_token"))
    return _transition_response("resume", addr, result)


def _handle_escalation_ack(request: dict) -> dict:
    """Close the daemon's pending freeze escalation — rung (c) of the usage escape hatch.

    Run-level, so it carries no address. The daemon owns the episode; this verb is the outside
    world's one-shot way to say "seen, I am on it" before the ladder wakes the human and pauses.
    """
    from harnessd import daemon as _daemon

    acked, detail = _daemon.ack_freeze_escalation()
    return {
        "ok": acked,
        "command": "escalation-ack",
        "errors": [] if acked else [f"escalation-ack: {detail}"],
        "warnings": [],
        "detail": detail,
    }


def _handle_gate_retry(request: dict) -> dict:
    """Retry a failed review gate after the parent/operator has repaired the review substrate."""
    addr = request.get("addr")
    result = chokepoint.retry_failed_gate(
        addr,
        expected_owner_token=request.get("expected_owner_token"),
        reason=request.get("reason") or "",
    )
    return _transition_response("gate-retry", addr, result)


def _handle_gate_accept(request: dict) -> dict:
    """Accept a parent-escalated gate candidate and finalize the held producer."""
    addr = request.get("addr")
    result = chokepoint.accept_escalated_gate(
        addr,
        resolver_address=request.get("resolver_address"),
        expected_parent_owner_token=request.get("expected_parent_owner_token"),
        verdict_notes=request.get("verdict_notes") or "",
    )
    if result is None:
        return {
            "ok": False,
            "command": "gate-accept",
            "addr": addr,
            "errors": [f"gate-accept: no unresolved gate escalation for {addr!r}"],
            "warnings": [],
            "binding": ledger.read_binding(addr),
        }
    return _transition_response("gate-accept", addr, result)


def _handle_gate_return(request: dict) -> dict:
    """Return a parent-escalated gate to its producer with a canonical ruling message."""
    addr = request.get("addr")
    result = chokepoint.return_escalated_gate(
        addr,
        resolver_address=request.get("resolver_address"),
        expected_parent_owner_token=request.get("expected_parent_owner_token"),
        verdict_notes=request.get("verdict_notes") or "",
    )
    if result is None:
        return {
            "ok": False,
            "command": "gate-return",
            "addr": addr,
            "errors": [f"gate-return: no unresolved gate escalation for {addr!r}"],
            "warnings": [],
            "binding": ledger.read_binding(addr),
        }
    return _transition_response("gate-return", addr, result)


def _handle_test_refresh_approve(request: dict) -> dict:
    """L3 approves a passed post-design acceptance refresh before implementation uses it."""
    addr = request.get("addr")
    result = chokepoint.approve_test_refresh(
        addr,
        approver_address=request.get("approver_address"),
        expected_parent_owner_token=request.get("expected_parent_owner_token"),
        notes=request.get("notes") or "",
    )
    if result is None:
        return {
            "ok": False,
            "command": "test-refresh-approve",
            "addr": addr,
            "errors": [f"test-refresh-approve: no such L4 node {addr!r}"],
            "warnings": [],
            "binding": None,
        }
    return _transition_response("test-refresh-approve", addr, result)


def _handle_intent_revise(request: dict) -> dict:
    """Ratify one fenced L1-owned intent-spec amendment."""
    addr = request.get("addr")
    result = chokepoint.revise_intent_spec(
        addr,
        target_address=request.get("target_address"),
        candidate_ref=request.get("candidate_ref"),
        reason=request.get("reason") or "",
        expected_owner_token=request.get("expected_owner_token"),
    )
    return _transition_response("intent-revise", addr, result)


def _is_escalated(addr: str, binding: dict) -> bool:
    escalated = binding.get("terminal_signal") == "ESCALATED"
    if escalated:
        return True
    sig = detector_signals.read_terminal_signal(binding, binding)
    return sig is not None and sig.get("signal") == "ESCALATED"


def _inbox_has_line(inbox: Path, **criteria) -> bool:
    try:
        for raw in inbox.read_text(encoding="utf-8").splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if all(line.get(key) == value for key, value in criteria.items() if value is not None):
                return True
    except OSError:
        return False
    return False


def _answer_artifact_path(addr: str, content: str, signal_identity: Optional[str]) -> Path:
    digest = hashlib.sha256(
        f"{addr}\0{signal_identity or ''}\0{content}".encode("utf-8")
    ).hexdigest()[:16]
    return addressing.node_dir(addr, ledger.RUNTIME_ROOT) / "answers" / f"escalation-answer-{digest}.md"


def _plan_alignment_decision_artifact_path(addr: str, decision: str, content: str) -> Path:
    digest = hashlib.sha256(
        f"{addr}\0{decision}\0{content}".encode("utf-8")
    ).hexdigest()[:16]
    return (
        addressing.node_dir(addr, ledger.RUNTIME_ROOT)
        / "plan-alignment"
        / f"plan-alignment-decision-{digest}.md"
    )


def _safe_coordination_segment(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    text = "".join(ch if ch in allowed else "-" for ch in str(value or "").strip())
    return text[:96] or "handoff"


def _coordination_decision_artifact_path(
    addr: str,
    handoff_id: str,
    decision: str,
    content: str,
) -> Path:
    digest = hashlib.sha256(
        f"{addr}\0{handoff_id}\0{decision}\0{content}".encode("utf-8")
    ).hexdigest()[:16]
    safe_id = _safe_coordination_segment(handoff_id)
    return (
        addressing.node_dir(addr, ledger.RUNTIME_ROOT)
        / "coordination"
        / f"coordination-decision-{safe_id}-{decision}-{digest}.md"
    )


def _coordination_note_artifact_path(
    addr: str,
    handoff_id: str,
    handoff_kind: str,
    content: str,
) -> Path:
    digest = hashlib.sha256(
        f"{addr}\0{handoff_id}\0{handoff_kind}\0{content}".encode("utf-8")
    ).hexdigest()[:16]
    safe_id = _safe_coordination_segment(handoff_id)
    return (
        addressing.node_dir(addr, ledger.RUNTIME_ROOT)
        / "coordination"
        / f"coordination-note-{safe_id}-{digest}.md"
    )


def _canonical_compat_artifact_path(
    sender: str,
    *,
    message_id: str,
    content: str,
) -> Path:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    safe_id = _safe_coordination_segment(message_id)
    return (
        addressing.messages_dir(sender, ledger.RUNTIME_ROOT)
        / f"{safe_id}-{digest}.md"
    )


def _write_canonical_compat_artifact(
    sender: str,
    *,
    message_id: str,
    content: str,
) -> Path:
    path = _canonical_compat_artifact_path(
        sender,
        message_id=message_id,
        content=content,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _handle_answer(request: dict) -> dict:
    """The F16 answer-injection verb (TRANSPORTS §5.3 primitive 3) — stamp, then wake the parent.

    (1) Fail-loud guards: content required; the node must exist; the node must actually be
        ESCALATED — read the binding stamp first, falling back to the durable fenced .signal
        artifact (the same fenced reader the watchdog uses) for the agent-signed-but-not-yet-
        journaled tick gap. Mirrors the _handle_kill fail-loud precedent.
    (2) Stamp ``terminal_note`` through executor.post_answer (the single writer). The ESCALATED
        stamp stays IN PLACE — the answer RIDES terminal_signal=ESCALATED + terminal_note.
    (3) The human->parent wake hop: append ONE pointer line to the PARENT's .inbox.<seat>.jsonl
        (the §3 multi-writer append log harnessd TAILS — not the single-writer ledger), which the
        existing ``inbox_has_unacked`` edge-trigger reads. The human sits ABOVE the parent, so a
        parentless L1 is woken ITSELF (the human IS L1's parent). An append failure is surfaced
        (the stamp is already durable) — never swallowed.
    """
    addr = request.get("addr")
    content = request.get("answer_content")
    if content is None:
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": ["answer requires answer_content (--text/--file)"],
            "binding": None,
        }

    binding = ledger.read_binding(addr)
    if binding is None:
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [f"no binding for node {addr!r}: nothing to answer"],
            "binding": None,
        }

    question_id = str(request.get("question_id") or "").strip()
    if question_id and question_id in (
        binding.get("fidelity_playback_owner_questions") or {}
    ):
        result = fidelity_playback.answer_question(
            addr,
            question_id=question_id,
            decision=request.get("decision"),
            note=content,
            answer_authority=request.get("answer_authority"),
            answer_actor=request.get("answer_actor"),
        )
        return {
            "ok": bool(result.get("ok")),
            "command": "answer",
            "addr": addr,
            **{
                key: value
                for key, value in result.items()
                if key not in {"ok"}
            },
        }
    # Fidelity REJECT needs to see an empty note so it can return its more specific typed
    # reason defect. Preserve the pre-Q6 empty-content refusal for every existing answer path.
    if not content:
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": ["answer requires answer_content (--text/--file)"],
            "binding": binding,
        }
    if question_id:
        return _handle_plan_alignment_owner_answer(request, binding)

    # The ESCALATED guard (fail-loud): the binding stamp (chokepoint.escalate's journal), else
    # the durable fenced .signal artifact (covers the agent-signed / watchdog-not-yet-ticked gap).
    if not _is_escalated(addr, binding):
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [f"node {addr!r} is not ESCALATED — nothing to answer"],
            "binding": binding,
        }

    result = _executor.post_answer(addr, answer=content)
    if not result.ok:
        return _transition_response("answer", addr, result)

    # The human->parent wake hop (stamp-then-wake, TRANSPORTS §5.3): the next-up node, or the
    # node ITSELF for a parentless L1 root.
    wake_target = binding.get("parent_address") or addr
    inbox = addressing.inbox_path(wake_target, ledger.RUNTIME_ROOT)
    line = {
        "from": "human",
        "type": "answer_posted",
        "child": addr,
        "message": f"human answer posted for {addr}, execute the decision-down",
        "ts": clock.now_utc(),
    }
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError as exc:
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [f"answer stamped (durable) but the parent wake append failed: {exc}"],
            "warnings": [],
            "binding": result.binding,
            "wake_target": wake_target,
        }

    response = _transition_response("answer", addr, result)
    response["wake_target"] = wake_target
    return response


def _handle_plan_alignment_owner_answer(request: dict, binding: dict) -> dict:
    """Answer one current elevate-only plan-alignment question on L1."""
    addr = request.get("addr")
    question_id = str(request.get("question_id") or "").strip()
    decision = str(request.get("decision") or "").strip().lower()
    content = str(request.get("answer_content") or "")
    if binding.get("level") != "L1":
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [
                "plan-alignment --question-id answers target the owning L1 binding"
            ],
            "binding": binding,
        }
    if not question_id or decision not in {"confirm", "reject"}:
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [
                "plan-alignment owner answer requires --question-id and "
                "--decision confirm|reject"
            ],
            "binding": binding,
        }
    questions = copy.deepcopy(binding.get("plan_alignment_owner_questions") or {})
    question = questions.get(question_id)
    if not isinstance(question, dict):
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [f"unknown plan-alignment owner question {question_id!r}"],
            "binding": binding,
        }
    child = ledger.read_binding(question.get("cell_for"))
    if (
        child is None
        or child.get("plan_alignment_semantic_evidence_sha256")
        != question.get("semantic_evidence_sha256")
        or child.get("plan_alignment_semantic_bundle_sha256")
        != question.get("cell_sha256")
    ):
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [
                f"plan-alignment owner question {question_id!r} drifted from current evidence"
            ],
            "binding": binding,
        }
    marker = Path(str(question.get("marker") or ""))
    if not marker.is_file() or notary.stamp(marker).get("sha256") != question.get(
        "marker_sha256"
    ):
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [
                f"plan-alignment owner question {question_id!r} marker drifted"
            ],
            "binding": binding,
        }
    if question.get("status") in {"confirmed", "rejected"}:
        if question.get("decision") == decision:
            return {
                "ok": True,
                "command": "answer",
                "addr": addr,
                "errors": [],
                "warnings": [],
                "binding": binding,
                "wake_target": addr,
                "question_id": question_id,
                "decision": decision,
                "answer_artifact": question.get("answer_artifact"),
            }
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [
                f"plan-alignment owner question {question_id!r} is already "
                f"{question.get('status')}"
            ],
            "binding": binding,
        }

    evidence_path = Path(str(question["semantic_evidence"]))
    answer_dir = evidence_path.parent / "owner-answers"
    answer_path = answer_dir / f"{question_id}.json"
    answer_payload = {
        "schema_version": 1,
        "question_id": question_id,
        "finding_fingerprint": question["finding_fingerprint"],
        "semantic_evidence_sha256": question["semantic_evidence_sha256"],
        "decision": decision,
        "note": content,
        "answered_at": clock.now_utc(),
    }
    try:
        answer_dir.mkdir(parents=True, exist_ok=True)
        store.atomic_replace(
            answer_path,
            lambda handle: (
                handle.write(json.dumps(answer_payload, indent=2, sort_keys=True)),
                handle.write("\n"),
            ),
        )
        answer_stamp = notary.stamp(answer_path, read_only=True)
    except OSError as exc:
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [f"could not write owner answer artifact {answer_path}: {exc}"],
            "binding": binding,
        }
    question.update(
        {
            "status": "confirmed" if decision == "confirm" else "rejected",
            "decision": decision,
            "answered_at": answer_payload["answered_at"],
            "answer_artifact": str(answer_path),
            "answer_sha256": answer_stamp.get("sha256"),
        }
    )
    questions[question_id] = question
    result = _executor.record_admission(
        addr,
        expected_owner_token=None,
        delta={"plan_alignment_owner_questions": questions},
        event="plan_alignment_owner_question_answered",
        summary=(
            f"human {decision}ed plan-alignment finding "
            f"{question['finding_fingerprint']} for {question['cell_for']}"
        ),
    )
    if result is None or not result.ok:
        return _transition_response(
            "answer",
            addr,
            result
            or _executor.TransitionResult(
                ok=False,
                errors=["owner answer could not be committed"],
                warnings=[],
                binding=ledger.read_binding(addr),
            ),
        )
    inbox = addressing.inbox_path(addr, ledger.RUNTIME_ROOT)
    line = {
        "from": "human",
        "type": "plan_alignment_owner_question_answered",
        "question_id": question_id,
        "finding_fingerprint": question["finding_fingerprint"],
        "decision": decision,
        "answer_artifact": str(answer_path),
        "message": (
            f"Owner {decision}ed plan-alignment finding "
            f"{question['finding_fingerprint']}; resume the gate verdict."
        ),
        "ts": clock.now_utc(),
    }
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except OSError as exc:
        return {
            "ok": False,
            "command": "answer",
            "addr": addr,
            "errors": [
                f"owner answer committed but the L1 wake append failed: {exc}"
            ],
            "binding": result.binding,
        }
    response = _transition_response("answer", addr, result)
    response.update(
        {
            "wake_target": addr,
            "question_id": question_id,
            "decision": decision,
            "answer_artifact": str(answer_path),
        }
    )
    return response


def _handle_answer_down(request: dict) -> dict:
    """Parent-to-child Branch-A answer-down for a live ESCALATED child.

    This is distinct from F16 ``answer``: that verb is the human->parent hop. Here the parent
    has already made the decision and the child must receive a durable pointer it can see by
    following the normal inbox wake contract.
    """
    addr = request.get("addr")
    content = request.get("answer_content")
    if not content:
        return {
            "ok": False,
            "command": "answer-down",
            "addr": addr,
            "errors": ["answer-down requires answer_content (--text/--file)"],
            "binding": None,
        }

    binding = ledger.read_binding(addr)
    if binding is None:
        return {
            "ok": False,
            "command": "answer-down",
            "addr": addr,
            "errors": [f"no binding for node {addr!r}: nothing to answer down"],
            "binding": None,
        }
    if binding.get("state") != "running":
        return {
            "ok": False,
            "command": "answer-down",
            "addr": addr,
            "errors": [
                f"answer-down Branch A requires a running child; {addr!r} is {binding.get('state')!r}"
            ],
            "binding": binding,
        }
    if not _is_escalated(addr, binding):
        return {
            "ok": False,
            "command": "answer-down",
            "addr": addr,
            "errors": [f"node {addr!r} is not ESCALATED — nothing to answer down"],
            "binding": binding,
        }

    actor = binding.get("parent_address")
    if not actor:
        return {
            "ok": False,
            "command": "answer-down",
            "addr": addr,
            "errors": [f"node {addr!r} has no parent_address for parent decision-down"],
            "binding": binding,
        }

    signal_identity = binding.get("signal_artifact_seen_at")
    decision_path = _answer_artifact_path(addr, content, signal_identity)
    try:
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "command": "answer-down",
            "addr": addr,
            "errors": [f"could not write answer-down decision artifact {decision_path}: {exc}"],
            "binding": binding,
        }

    artifact = str(decision_path)
    question_id = messages.escalation_message_id(signal_identity)
    try:
        # Ensure old ESCALATED state has the canonical question even when this IPC call races the
        # daemon's read-side compatibility sweep.
        signal_path = addressing.signal_path(addr, ledger.RUNTIME_ROOT)
        if not signal_path.is_file():
            # A pre-3a binding may carry only the already-adopted signal identity (tests and crash
            # recovery both exercise this shape). Materialize a sender-owned compatibility pointer;
            # it does not pretend to reconstruct missing question prose.
            signal_path = _write_canonical_compat_artifact(
                addr,
                message_id=question_id,
                content=(
                    "# Legacy escalated question\n\n"
                    f"Signal identity: {signal_identity or 'unknown'}\n"
                ),
            )
        messages.submit_compat_message(
            addr,
            target=actor,
            message_id=question_id,
            artifact=signal_path,
            marker=signal_path,
            summary=f"{addr} escalated and is waiting for an answer.",
            needs_answer=True,
            metadata={"kind": "legacy_escalation", "signal_identity": signal_identity},
            tags=["escalation"],
            # If this call is the compatibility adopter, the question is being answered in this
            # same operation; do not emit a stale open-question wake to the parent.
            deliver_pointer=False,
            runtime_root=ledger.RUNTIME_ROOT,
        )
        answer_id = (
            f"legacy-answer-{hashlib.sha256(f'{question_id}\\0{content}'.encode()).hexdigest()[:24]}"
        )
        canonical_artifact = _write_canonical_compat_artifact(
            actor,
            message_id=answer_id,
            content=content,
        )
        messages.submit_compat_message(
            actor,
            target=addr,
            message_id=answer_id,
            artifact=canonical_artifact,
            marker=canonical_artifact,
            summary=f"Parent answer for escalation {question_id}.",
            metadata={"kind": "legacy_answer_down", "decision_artifact": artifact},
            answers_question={"asker_address": addr, "message_id": question_id},
            deliver_pointer=True,
            runtime_root=ledger.RUNTIME_ROOT,
        )
    except Exception as exc:
        return {
            "ok": False,
            "command": "answer-down",
            "addr": addr,
            "errors": [f"canonical answer message failed: {exc}"],
            "binding": ledger.read_binding(addr) or binding,
        }
    binding = ledger.read_binding(addr) or binding
    result = _executor.post_answer(
        addr,
        answer=content,
        route="parent_to_child_alive",
        actor=actor,
        artifact=artifact,
        signal_artifact_seen_at=signal_identity,
        event="parent_answer_posted",
        summary=(
            "parent decision-down posted into a live ESCALATED child "
            "(decision artifact + child inbox wake + canonical answered_at; TRANSPORTS §4.1)"
        ),
    )
    if not result.ok:
        return _transition_response("answer-down", addr, result)

    response = _transition_response("answer-down", addr, result)
    response["wake_target"] = addr
    response["answer_route"] = "parent_to_child_alive"
    response["decision_artifact"] = artifact
    return response


def _handle_plan_alignment_decision(request: dict) -> dict:
    """L1 -> L2 decision-down for the nonterminal plan-alignment handoff."""
    addr = request.get("addr")
    decision = str(request.get("decision") or "").strip().lower()
    content = request.get("decision_content")
    if decision not in {"pass", "fail"}:
        return {
            "ok": False,
            "command": "plan-alignment-decision",
            "addr": addr,
            "errors": ["plan-alignment-decision requires --decision pass|fail"],
            "binding": None,
        }
    if not content:
        return {
            "ok": False,
            "command": "plan-alignment-decision",
            "addr": addr,
            "errors": ["plan-alignment-decision requires decision content (--text/--file)"],
            "binding": None,
        }
    binding = ledger.read_binding(addr)
    if binding is None:
        return {
            "ok": False,
            "command": "plan-alignment-decision",
            "addr": addr,
            "errors": [f"no binding for node {addr!r}: nothing to decide"],
            "binding": None,
        }
    if binding.get("state") != "running":
        return {
            "ok": False,
            "command": "plan-alignment-decision",
            "addr": addr,
            "errors": [
                f"plan-alignment decision requires a running L2 child; {addr!r} is {binding.get('state')!r}"
            ],
            "binding": binding,
        }
    if binding.get("plan_alignment_state") not in {
        chokepoint.PLAN_ALIGNMENT_STATE_READY,
        chokepoint.PLAN_ALIGNMENT_STATE_DECISION_POSTED,
    }:
        return {
            "ok": False,
            "command": "plan-alignment-decision",
            "addr": addr,
            "errors": [
                f"{addr!r} has not submitted a plan-alignment-ready package"
            ],
            "binding": binding,
        }
    actor = binding.get("parent_address")
    if not actor:
        return {
            "ok": False,
            "command": "plan-alignment-decision",
            "addr": addr,
            "errors": [f"node {addr!r} has no parent_address for plan-alignment decision"],
            "binding": binding,
        }
    if decision == "pass":
        owner = ledger.read_binding(actor) or {}
        blockers = plan_alignment_cell.plan_alignment_pass_blockers(
            binding,
            owner,
        )
        if blockers:
            return {
                "ok": False,
                "command": "plan-alignment-decision",
                "addr": addr,
                "errors": list(blockers),
                "binding": binding,
            }

    decision_path = _plan_alignment_decision_artifact_path(addr, decision, content)
    try:
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "command": "plan-alignment-decision",
            "addr": addr,
            "errors": [f"could not write plan-alignment decision artifact {decision_path}: {exc}"],
            "binding": binding,
        }

    artifact = str(decision_path)
    if binding.get("plan_alignment_state") == chokepoint.PLAN_ALIGNMENT_STATE_DECISION_POSTED:
        if (
            binding.get("plan_alignment_decision") == decision
            and binding.get("plan_alignment_decision_artifact") == artifact
        ):
            result = _executor.TransitionResult(ok=True, errors=[], warnings=[], binding=binding)
        else:
            return {
                "ok": False,
                "command": "plan-alignment-decision",
                "addr": addr,
                "errors": [
                    f"{addr!r} already has a different plan-alignment decision posted"
                ],
                "binding": binding,
            }
    else:
        result = _executor.record_admission(
            addr,
            expected_owner_token=None,
            delta={
                "plan_alignment_state": chokepoint.PLAN_ALIGNMENT_STATE_DECISION_POSTED,
                "plan_alignment_decision": decision,
                "plan_alignment_decision_artifact": artifact,
                "plan_alignment_decision_at": clock.now_utc(),
                "plan_alignment_decision_actor": actor,
            },
            event="plan_alignment_decision_posted",
            summary=(
                f"L1 plan-alignment decision {decision.upper()} posted for {addr}; "
                "child wakes to continue or repair"
            ),
        )
    if not result.ok:
        return _transition_response("plan-alignment-decision", addr, result)

    inbox = addressing.inbox_path(addr, ledger.RUNTIME_ROOT)
    if not _inbox_has_line(
        inbox,
        type="plan_alignment_decision",
        phase="plan_alignment",
        decision=decision,
        decision_artifact=artifact,
    ):
        line = {
            "from": "harnessd",
            "type": "plan_alignment_decision",
            "phase": "plan_alignment",
            "child": addr,
            "decision": decision,
            "decision_actor": actor,
            "decision_artifact": artifact,
            "message": (
                "L1 plan-alignment gate PASS posted; read the decision artifact and continue."
                if decision == "pass"
                else "L1 plan-alignment gate FAIL posted; read the decision artifact, repair the package, and resubmit."
            ),
            "ts": clock.now_utc(),
        }
        try:
            inbox.parent.mkdir(parents=True, exist_ok=True)
            with inbox.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError as exc:
            return {
                "ok": False,
                "command": "plan-alignment-decision",
                "addr": addr,
                "errors": [f"decision stamped (durable) but the child wake append failed: {exc}"],
                "warnings": [],
                "binding": result.binding,
                "wake_target": addr,
                "decision_artifact": artifact,
            }

    response = _transition_response("plan-alignment-decision", addr, result)
    response["wake_target"] = addr
    response["decision"] = decision
    response["decision_artifact"] = artifact
    return response


def _handle_coordination_note(request: dict) -> dict:
    """Parent -> child nonterminal coordination notice."""
    addr = request.get("addr")
    handoff_id = str(request.get("handoff_id") or "").strip()
    handoff_kind = str(request.get("handoff_kind") or "").strip()
    content = request.get("note_content")
    if not handoff_id:
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": ["coordination-note requires --handoff-id"],
            "binding": None,
        }
    if handoff_kind not in chokepoint.COORDINATION_HANDOFF_ALLOWED_KINDS:
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": [
                "coordination-note requires --kind in "
                f"{sorted(chokepoint.COORDINATION_HANDOFF_ALLOWED_KINDS)}"
            ],
            "binding": None,
        }
    if not content:
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": ["coordination-note requires note content (--text/--file)"],
            "binding": None,
        }
    binding = ledger.read_binding(addr)
    if binding is None:
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": [f"no binding for node {addr!r}: nothing to notify"],
            "binding": None,
        }
    if binding.get("state") != "running":
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": [
                f"coordination note requires a running child; {addr!r} is {binding.get('state')!r}"
            ],
            "binding": binding,
        }
    actor = binding.get("parent_address")
    if not actor:
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": [f"node {addr!r} has no parent_address for coordination note"],
            "binding": binding,
        }
    records = binding.get("coordination_handoffs") or {}
    if not isinstance(records, dict):
        records = {}
    existing = records.get(handoff_id) or {}
    artifact_path = _coordination_note_artifact_path(addr, handoff_id, handoff_kind, content)
    artifact = str(artifact_path)
    if existing and not (
        existing.get("state") == chokepoint.COORDINATION_HANDOFF_STATE_NOTICE_POSTED
        and existing.get("artifact") == artifact
    ):
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": [
                f"{addr!r} coordination handoff {handoff_id!r} already exists; use a fresh handoff_id"
            ],
            "binding": binding,
        }
    try:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": [f"could not write coordination note artifact {artifact_path}: {exc}"],
            "binding": binding,
        }
    try:
        canonical_artifact = _write_canonical_compat_artifact(
            actor,
            message_id=handoff_id,
            content=content,
        )
        messages.submit_compat_message(
            actor,
            target=addr,
            message_id=handoff_id,
            artifact=canonical_artifact,
            marker=canonical_artifact,
            summary=str(request.get("summary") or "Parent coordination note posted."),
            metadata={"kind": handoff_kind, "legacy": "coordination_note"},
            deliver_pointer=True,
            runtime_root=ledger.RUNTIME_ROOT,
        )
    except Exception as exc:
        return {
            "ok": False,
            "command": "coordination-note",
            "addr": addr,
            "errors": [f"canonical coordination message failed: {exc}"],
            "binding": ledger.read_binding(addr) or binding,
        }
    return {
        "ok": True,
        "command": "coordination-note",
        "addr": addr,
        "errors": [],
        "warnings": [],
        "binding": ledger.read_binding(addr) or binding,
        "wake_target": addr,
        "handoff_id": handoff_id,
        "artifact": artifact,
    }


def _handle_coordination_decision(request: dict) -> dict:
    """Parent -> child decision-down for a nonterminal coordination handoff."""
    addr = request.get("addr")
    handoff_id = str(request.get("handoff_id") or "").strip()
    decision = str(request.get("decision") or "").strip().lower()
    content = request.get("decision_content")
    allowed = {"ack", "approve", "reject", "revise", "guidance"}
    if not handoff_id:
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": ["coordination-decision requires --handoff-id"],
            "binding": None,
        }
    if decision not in allowed:
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [f"coordination-decision requires --decision in {sorted(allowed)}"],
            "binding": None,
        }
    if not content:
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": ["coordination-decision requires decision content (--text/--file)"],
            "binding": None,
        }
    binding = ledger.read_binding(addr)
    if binding is None:
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [f"no binding for node {addr!r}: nothing to decide"],
            "binding": None,
        }
    if binding.get("state") != "running":
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [
                f"coordination decision requires a running child; {addr!r} is {binding.get('state')!r}"
            ],
            "binding": binding,
        }
    records = binding.get("coordination_handoffs") or {}
    if not isinstance(records, dict):
        records = {}
    record = records.get(handoff_id)
    canonical_question = (binding.get("messages") or {}).get(handoff_id)
    if not isinstance(record, dict) and not isinstance(canonical_question, dict):
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [f"{addr!r} has no coordination handoff {handoff_id!r}"],
            "binding": binding,
        }
    record = dict(record or {})
    if canonical_question:
        record.setdefault(
            "handoff_kind",
            (canonical_question.get("metadata") or {}).get("kind"),
        )
        record.setdefault("response_required", bool(canonical_question.get("needs_answer")))
    actor = binding.get("parent_address")
    if not actor:
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [f"node {addr!r} has no parent_address for coordination decision"],
            "binding": binding,
        }

    decision_path = _coordination_decision_artifact_path(addr, handoff_id, decision, content)
    artifact = str(decision_path)
    if record.get("state") == chokepoint.COORDINATION_HANDOFF_STATE_DECISION_POSTED and not (
        record.get("decision") == decision and record.get("decision_artifact") == artifact
    ):
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [
                f"{addr!r} coordination handoff {handoff_id!r} already has a different decision posted"
            ],
            "binding": binding,
        }
    try:
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [f"could not write coordination decision artifact {decision_path}: {exc}"],
            "binding": binding,
        }

    canonical_answer_ref = (
        {"asker_address": addr, "message_id": handoff_id}
        if record.get("response_required")
        else None
    )
    canonical_id = (
        f"decision-{handoff_id}-"
        f"{hashlib.sha256(f'{decision}\\0{content}'.encode()).hexdigest()[:16]}"
    )
    try:
        if canonical_answer_ref and not canonical_question:
            messages.submit_compat_message(
                addr,
                target=actor,
                message_id=handoff_id,
                artifact=record.get("artifact"),
                marker=record.get("marker_artifact") or record.get("artifact"),
                summary=record.get("summary") or f"Legacy coordination handoff {handoff_id}.",
                needs_answer=True,
                metadata={
                    "kind": record.get("handoff_kind"),
                    "phase": record.get("phase"),
                    "legacy": "coordination_handoff",
                },
                runtime_root=ledger.RUNTIME_ROOT,
            )
        canonical_artifact = _write_canonical_compat_artifact(
            actor,
            message_id=canonical_id,
            content=content,
        )
        messages.submit_compat_message(
            actor,
            target=addr,
            message_id=canonical_id,
            artifact=canonical_artifact,
            marker=canonical_artifact,
            summary=f"Coordination decision {decision.upper()} for {handoff_id}.",
            metadata={
                "kind": record.get("handoff_kind"),
                "decision": decision,
                "legacy": "coordination_decision",
            },
            answers_question=canonical_answer_ref,
            deliver_pointer=True,
            runtime_root=ledger.RUNTIME_ROOT,
        )
    except Exception as exc:
        return {
            "ok": False,
            "command": "coordination-decision",
            "addr": addr,
            "errors": [f"canonical coordination decision failed: {exc}"],
            "binding": ledger.read_binding(addr) or binding,
        }
    return {
        "ok": True,
        "command": "coordination-decision",
        "addr": addr,
        "errors": [],
        "warnings": [],
        "binding": ledger.read_binding(addr) or binding,
        "wake_target": addr,
        "handoff_id": handoff_id,
        "decision": decision,
        "decision_artifact": artifact,
    }


def _transition_response(command: str, addr, result) -> dict:
    """Shape a TransitionResult into the JSON response (ok / errors / warnings / binding)."""
    return {
        "ok": bool(result.ok),
        "command": command,
        "addr": addr,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "binding": result.binding,
    }


# ---------------------------------------------------------------------------
# Read handlers — return ledger state (§4.5 read-only surface). These read through the ledger's keyed
# map; they take the shared lock implicitly via the read path (no whole-map replace).
# ---------------------------------------------------------------------------


def _handle_show(request: dict) -> dict:
    """Return the REAL ledger state for the addressed node (or ``null`` if absent) — a §4.5 read."""
    addr = request.get("addr")
    binding = ledger.read_binding(addr)
    return {"ok": True, "command": "show", "addr": addr, "binding": binding}


def _handle_turn_hook_adopt(request: dict) -> dict:
    """Wake the existing daemon adoption seam for one already-durable exact raw event."""
    addr = str(request.get("addr") or "")
    event_id = str(request.get("event_id") or "")
    if not addr or not event_id:
        return {
            "ok": False,
            "command": "turn-hook-adopt",
            "addr": addr,
            "event_id": event_id,
            "errors": ["turn-hook-adopt requires addr and exact event_id"],
        }
    binding = ledger.read_binding(addr)
    if binding is None:
        return {
            "ok": False,
            "command": "turn-hook-adopt",
            "addr": addr,
            "event_id": event_id,
            "errors": [f"no binding for {addr!r}"],
        }
    from harnessd import daemon as _daemon
    from harnessd.spawn import tmux as _tmux

    result = _daemon._adopt_turn_state_for_seat(
        _executor,
        _tmux,
        addr,
        binding,
        target_event_id=event_id,
    )
    ok = bool(result.get("found")) and (
        result.get("response_event_id") == event_id
        or not result.get("response_required", False)
    )
    return {
        "ok": ok,
        "command": "turn-hook-adopt",
        "addr": addr,
        "event_id": event_id,
        "adoption": result,
        "errors": [] if ok else [f"raw hook event {event_id!r} was not adopted"],
    }


def _handle_tree(request: dict) -> dict:
    """Return the WHOLE binding map — the operator fleet/tree read (§4.5 read-only; review COMP-4). The
    CLI renders it as an indented supervision tree for situational awareness during a run."""
    return {"ok": True, "command": "tree", "nodes": ledger.all_nodes()}


def _handle_next_seq(request: dict) -> dict:
    """Return the next monotonic WAL ``seq`` (read-only — derived from the WAL on load, §4.5)."""
    return {"ok": True, "command": "next-seq", "next_seq": ledger.next_seq()}


def _handle_validate(request: dict) -> dict:
    """A whole-ledger admission scan (read-only): validate every committed binding snapshot.

    PURE: the committed-snapshot validator writes nothing (§4.2). Each node receives exactly the
    primary and related binding effects that recovery projects from the WAL; its binding watermark
    selects the applied writer effect. A generation-zero planned registration may have no effect,
    but it never borrows another node's transition.
    """
    wal = ledger.load_wal()
    nodes = ledger.all_nodes()
    effects_by_node = _reconcile.binding_effects_by_node(wal)
    errors: list[str] = []
    warnings: list[str] = []
    for addr, binding in nodes.items():
        tail = effects_by_node.get(addr, [])
        node_errors, node_warnings = validate.validate_committed_snapshot(binding, tail)
        errors.extend(f"{addr}: {message}" for message in node_errors)
        warnings.extend(f"{addr}: {message}" for message in node_warnings)
    return {"ok": not errors, "command": "validate", "errors": errors, "warnings": warnings}


def _handle_reconcile_inspect(request: dict) -> dict:
    """A DRY reconcile inspect (read-only, FORK-INSPECT-DRY): what a reconcile WOULD do, no write.

    Replays the WAL into an in-memory map and classifies divergence WITHOUT persisting or driving the
    executor — a read-only divergence verdict (§4.5: read-only commands may take the shared lock
    directly). The real reconcile sweep is the daemon's poll loop, not a CLI-triggered mutation.
    """
    bindings = ledger.all_nodes()
    wal = ledger.load_wal()
    replayed = _reconcile.replay_wal(bindings, wal)
    diverged = [
        addr
        for addr, replayed_binding in replayed.items()
        if bindings.get(addr) != replayed_binding
    ]
    return {
        "ok": True,
        "command": "reconcile-inspect",
        "nodes": sorted(replayed),
        "would_replay": sorted(diverged),
    }


_DISPATCH = {
    # Mutating (exclusive lock, routed through the single writer — §4.5).
    "transition": _handle_transition,
    "message": _handle_message,
    "kill": _handle_kill,
    "spawn": _handle_spawn,
    "service-outbox": _handle_service_outbox,
    "fidelity-playback": _handle_fidelity_playback,
    "promote": _handle_promote,
    "merge": _handle_merge,
    "pause": _handle_pause,
    "resume": _handle_resume,
    "escalation-ack": _handle_escalation_ack,
    "gate-retry": _handle_gate_retry,
    "gate-accept": _handle_gate_accept,
    "gate-return": _handle_gate_return,
    "test-refresh-approve": _handle_test_refresh_approve,
    "intent-revise": _handle_intent_revise,
    "answer": _handle_answer,
    "answer-down": _handle_answer_down,
    "plan-alignment-decision": _handle_plan_alignment_decision,
    "coordination-note": _handle_coordination_note,
    "coordination-decision": _handle_coordination_decision,
    "turn-hook-adopt": _handle_turn_hook_adopt,
    # Read-only (shared lock — §4.5).
    "show": _handle_show,
    "tree": _handle_tree,
    "next": _handle_next_seq,
    "next-seq": _handle_next_seq,
    "validate": _handle_validate,
    "reconcile-inspect": _handle_reconcile_inspect,
}


# ---------------------------------------------------------------------------
# serve_one — the BOUNDED single-accept primitive (the §2.12 poll_once analogue for IPC). A test drives
# exactly ONE accept/handle; production loops it in serve_forever. NEVER an unbounded loop in a test path.
# ---------------------------------------------------------------------------


def _journal_ipc_request_failed(stage: str, error: str, request=None) -> None:
    """Best-effort ``ipc_request_failed`` run-ledger row (RR-1): a control-plane fault is VISIBLE.

    Routed through ``executor.journal`` (the SWL-01 locked append). The row is keyed to the
    request's ``addr`` when it names an EXISTING binding (so the fault is legible per node), else
    to no node (a malformed request carries no trustworthy address — a row keyed to a nonexistent
    address would be reconstructed into a phantom binding by boot replay). Swallows its own
    failures — the journal must never re-kill the serve loop it exists to keep alive.
    """
    try:
        addr = request.get("addr") if isinstance(request, dict) else None
        node_address = addr if (addr and ledger.read_binding(addr) is not None) else None
        command = request.get("command") if isinstance(request, dict) else None
        _executor.journal(
            node_address,
            event="ipc_request_failed",
            binding_delta={"stage": stage, "command": command, "addr": addr, "error": error},
            summary=f"IPC request failed at {stage}: {error} (command={command!r}, addr={addr!r})",
        )
    except Exception:  # noqa: BLE001 — best-effort: the journal never crashes the control plane
        pass


def serve_one(listener: socket.socket, *, handler=handle_request) -> Optional[dict]:
    """Accept EXACTLY ONE connection on ``listener``, handle the framed request, write the response.

    The single drivable IPC step (mirrors ``daemon.poll_once``): accept one connection, read the
    request to EOF, run ``handler`` (the lock-held mutator/reader through the executor), write the JSON
    response back, close the connection, and RETURN the request handled (or None on a clean
    accept-then-EOF with no payload). There is NO loop here — this is the bounded unit the unbounded
    ``serve_forever`` composes, and the unit a test drives one step at a time.

    Takes the ``listener`` it accepts on as an explicit argument (no implicit global serve-forever): the
    caller owns the socket's lifecycle (bind / listen / close).

    HARDENED PER CONNECTION (RR-1) — a client must not be able to kill the daemon's control plane:
      * a STALLED read (IPC-DEADLOCK-2026-08-01) — the peer produces neither a byte nor EOF within
        ``REQUEST_IDLE_TIMEOUT_S`` -> the same structured-error + journal treatment at stage
        ``read``, the connection is closed, and the loop RETURNS TO ``accept``. This is the ONE
        fault class that used to be unbounded, and being unbounded in a single-threaded accept loop
        made it a total control-plane outage rather than one failed request;
      * non-JSON bytes -> a structured ``{ok: false, errors}`` response + an ``ipc_request_failed``
        journal row (the request is NOT dispatched);
      * a handler exception -> the same structured-error + journal treatment (the daemon-side mirror
        of F2r's never-a-traceback client rule);
      * a ``sendall`` failure (the peer hung up before reading — BrokenPipe/ConnectionReset, both
        OSError) is a PER-CONNECTION fault, journaled and swallowed — except for the intentional
        fire-and-forget ``turn-hook-adopt`` caller, which never reads the response and therefore has
        no response failure to record. It must never be misread as listener shutdown (the pre-fix
        ``except OSError: return`` in serve_forever did exactly that).
    Only ``listener.accept()`` is left unguarded — its OSError IS the listener-lifecycle signal
    ``serve_forever`` routes.
    """
    conn, _addr = listener.accept()
    request: Optional[dict] = None
    response: Optional[dict] = None
    with conn:
        _harden_connection(conn)
        try:
            raw = _recv_all(conn)
        except TimeoutError as exc:
            # The peer stalled mid-frame (the 2026-07-31 outage shape). Abandon THIS connection and
            # return to accept — never dispatch a truncated request, never wait on this peer again.
            raw = b""
            response = {
                "ok": False,
                "errors": [
                    f"request read timed out after {REQUEST_IDLE_TIMEOUT_S:g}s with no EOF "
                    f"({exc or 'timed out'}): the connection was abandoned so the daemon "
                    "could keep serving other clients"
                ],
            }
            _journal_ipc_request_failed("read", f"{type(exc).__name__}: request read timed out")
        if response is None:
            try:
                request = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (ValueError, UnicodeDecodeError) as exc:
                # ValueError covers json.JSONDecodeError. Malformed bytes — structured abort, no dispatch.
                response = {
                    "ok": False,
                    "errors": [f"malformed request: not valid JSON ({exc})"],
                }
                _journal_ipc_request_failed("decode", str(exc))
                request = None
            else:
                try:
                    response = handler(request)
                except Exception as exc:  # noqa: BLE001 — a handler fault is per-request, never fatal
                    response = {
                        "ok": False,
                        "command": request.get("command") if isinstance(request, dict) else None,
                        "errors": [f"internal error handling request: {type(exc).__name__}: {exc}"],
                    }
                    _journal_ipc_request_failed("handle", f"{type(exc).__name__}: {exc}", request)
        try:
            conn.sendall(json.dumps(response).encode("utf-8"))
        except OSError as exc:
            # The peer disconnected before reading the response — per-connection, NOT listener-closed.
            # turn-hook-adopt is deliberately fire-and-forget: its caller closes without reading, so
            # a broken response leg is expected transport shape rather than an IPC request failure.
            if not (
                isinstance(request, dict)
                and request.get("command") == "turn-hook-adopt"
            ):
                _journal_ipc_request_failed(
                    "respond", f"{type(exc).__name__}: {exc}", request
                )
    return request


def serve_forever(listener: socket.socket, *, handler=handle_request) -> None:
    """The production live-run IPC loop — loops ``serve_one`` until the listener closes.

    The daemon assembles + binds the listener at boot and runs this in its serve thread. It is the
    unbounded composition of the BOUNDED ``serve_one`` (the single drivable step the tests exercise).

    FAULT DISCIPLINE (RR-1): the pre-fix loop caught ONLY OSError and RETURNED — so a client that
    disconnected before reading its response (BrokenPipeError IS OSError) ended the accept loop
    CLEANLY and permanently, and any non-OSError (bad JSON, a handler bug) killed the thread raw:
    either way pause/resume/answer/kill/promote/spawn died silently until a daemon restart. Now:
      * listener-CLOSED (the one legitimate shutdown signal: ``accept`` raises OSError with the
        socket's fileno already -1) -> exit the live-run loop cleanly;
      * ANY other exception is a per-connection fault -> journal ``ipc_request_failed`` (best-effort)
        and CONTINUE accepting. serve_one already contains decode/handler/sendall faults itself;
        this catch is the backstop.
    """
    while True:
        try:
            serve_one(listener, handler=handler)
        except OSError as exc:
            if listener.fileno() == -1:
                # The listener was closed (shutdown) — exit the live-run loop cleanly.
                return
            _journal_ipc_request_failed("serve", f"{type(exc).__name__}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — the control plane must outlive any one connection
            _journal_ipc_request_failed("serve", f"{type(exc).__name__}: {exc}")
            continue
