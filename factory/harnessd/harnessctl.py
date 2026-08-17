"""harnessctl — the OPERATOR CLI. A CLIENT, NEVER a writer.

Authoritative sources:
  - IMPLEMENTATION-PLAN §3 module table (harnessctl.py row, L65): "CLI client — NOT a writer. Sends
    requests to the running daemon over a local socket/FIFO; the daemon performs the mutation inside
    the one lock. Read-only commands (show/next/validate/reconcile-inspect) may take the shared lock
    directly." GENERALIZE: the recovered ``build_parser`` L1613-1696 subcommand structure -> node-addressed.
  - IMPLEMENTATION-PLAN §2.x / Increment-13 Done-test (L800-803): node-addressed subcommands
    (spawn/transition/show/reconcile-inspect/kill) over a local socket/FIFO to the daemon; a mutation
    via harnessctl is performed BY THE DAEMON inside the one lock (not by the CLI process); a read
    command returns ledger state.
  - DAEMON §4.3: "CLIs are clients, not writers. A harnessctl command sends a request to the running
    daemon (over a local socket/fifo), and the daemon performs the mutation inside the one lock." §4.5:
    read-only (shared lock) = show / next / validate / reconcile-inspect; mutating (exclusive lock) =
    transition / claim / collapse-kill.

THE CARDINAL RULE (§4.3): this module is a CLIENT. It does arg parsing + socket I/O ONLY. It NEVER
imports or calls the single-writer primitives (the ledger's whole-map writer, the executor's
state-changer, the spawn-chokepoint mutators). Every mutation is serialized into a request dict and
shipped over the local unix socket to the run-scoped daemon, which performs it inside the one EX lock.
The client prints the JSON response to stdout and returns an exit code (0 on ``ok``, nonzero otherwise).

BUILDER DECISIONS (stated in the build report):

  * THE SOCKET PATH (FORK-SOCKET-PATH). The daemon's IPC socket lives at
    ``<RUNTIME_ROOT>/.harnessd/harnessd.sock`` by default (alongside the §2.3 runtime.json / status.json
    under ``.harnessd/``). The client resolves the path in precedence order: an explicit
    ``main(argv, socket_path=...)`` kwarg, then a ``--socket <path>`` flag, then the ``HARNESSD_SOCKET``
    env var, then the RUNTIME_ROOT default. This keeps the wiring detail un-over-constrained (the §2.x
    plan names "a local socket/FIFO", not an exact path).

  * THE PRINT-JSON / EXIT-CODE CONVENTION. The client prints the daemon's response dict as one JSON line
    to stdout and returns ``0`` when ``response["ok"]`` is truthy, else ``2`` (a command-level abort —
    a daemon-reported abort OR a client-side input error such as an unreadable ``--brief``/``--file``
    input file, where the daemon is never contacted). A transport failure (no daemon reachable, or a
    garbled non-JSON response — the daemon did not speak the protocol) returns ``3`` after printing a
    JSON error line — the client cannot perform the mutation itself, so a failed transport is a hard
    failure, never a silent local write and NEVER a traceback.

  * NODE-ADDRESSED SUBCOMMANDS. Every command carries the node address as a positional ``addr`` (the
    GENERALIZE of the recovered ``build_parser`` subcommand structure). The read surface (show / next /
    validate / reconcile-inspect) and the mutation surface (spawn / transition / kill) share the
    node-addressed shape; the request dict is built from the parsed namespace and shipped as-is.
"""

from __future__ import annotations

import argparse
import json
import os
import socket as socket_mod
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import commissioning, ledger, lifecycle, observability, states


# The IPC socket lives under the runtime's ``.harnessd/`` dir (alongside runtime.json / status.json).
SOCKET_FILENAME: str = "harnessd.sock"
SOCKET_ENV_VAR: str = "HARNESSD_SOCKET"


# ---------------------------------------------------------------------------
# build_parser — the node-addressed argparse CLI (GENERALIZE of the recovered build_parser L1613-1696).
# ---------------------------------------------------------------------------


def render_tree(nodes: dict) -> str:
    """Render the binding map as an indented supervision TREE (the operator fleet view, COMP-4).

    Each line: ``<indent><address>  [<level>] <state>/<liveness>``. Children are indented under their
    ``parent_address``; roots (parentless) are the top level. An ORPHAN (parent not in the map) is shown
    at the top level with a marker — never silently dropped (a missing node would hide a real gap). Sorted
    by address for stable output.
    """
    if not nodes:
        return "(no nodes)"
    children: dict = {}
    roots = []
    for addr, b in nodes.items():
        parent = (b or {}).get("parent_address")
        if parent and parent in nodes:
            children.setdefault(parent, []).append(addr)
        else:
            roots.append(addr)  # a true root (parent None) OR an orphan (parent absent from the map)

    lines: list[str] = []

    def _emit(addr: str, depth: int) -> None:
        b = nodes.get(addr) or {}
        level = b.get("level", "?")
        state = b.get("state", "?")
        liveness = b.get("liveness_state", "?")
        parent = b.get("parent_address")
        orphan = bool(parent) and parent not in nodes
        marker = "  ⚠orphan(parent missing)" if orphan else ""
        lines.append(f"{'  ' * depth}{addr}  [{level}] {state}/{liveness}{marker}")
        for child in sorted(children.get(addr, [])):
            _emit(child, depth + 1)

    for root in sorted(roots):
        _emit(root, 0)
    return "\n".join(lines)


def _add_addr(subparser) -> None:
    """Attach the node-address positional shared by every node-addressed subcommand (parse+route)."""
    subparser.add_argument(
        "addr",
        help="the node address (e.g. 'proj/widget#exec') this command targets",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the node-addressed CLI parser (§3 GENERALIZE of build_parser L1613-1696 -> node-addressed).

    Subcommands (Increment-13 Done-test L800-803 + §4.5 read-only surface):
      reads  (§4.5):  show / next-seq / validate / reconcile-inspect — return ledger state.
      writes (§4.3):  spawn / transition / kill — serialized into a request, performed by the daemon.

    The parser sets ``args.command`` to the subcommand name (the route) and carries the node ``addr``
    positional on every node-addressed command. A ``--socket`` flag overrides the daemon socket path.
    """
    parser = argparse.ArgumentParser(
        prog="harnessctl",
        description=(
            "harnessctl — the operator CLI. A CLIENT, not a state writer: mutations are sent to the live "
            "daemon over a local socket and performed inside the one lock (DAEMON §4.3)."
        ),
    )
    parser.add_argument(
        "--socket",
        dest="socket_path",
        default=None,
        help="path to the daemon IPC socket (default: <RUNTIME_ROOT>/.harnessd/harnessd.sock or $HARNESSD_SOCKET)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    # --- Run-scoped operator entry/helpers -----------------------------------------------------
    start = subparsers.add_parser(
        "start",
        help="start one run-scoped daemon/build and attach to L1 (lifecycle -> launchd)",
    )
    start.add_argument("--build-id", default=None)
    start.add_argument("--runtime-root", default=None)
    intake = start.add_mutually_exclusive_group()
    intake.add_argument("--intake", dest="initial_intake", default=None)
    intake.add_argument("--intake-file", dest="intake_file", default=None)
    start.add_argument(
        "--review-panel-arm",
        dest="review_panel_arms",
        action="append",
        default=None,
        metavar="<module-address-glob>=<axis>[,<axis>...]",
        help=(
            "commission an ordered L3 module review panel; repeat for mixed arms "
            "(first matching declaration wins)"
        ),
    )
    start.add_argument("--wait-seconds", type=float, default=30.0)
    start.add_argument(
        "--no-attach",
        action="store_true",
        help="return after L1 is confirmed live (automation/tests); normal start attaches",
    )

    status = subparsers.add_parser(
        "status",
        help="show the live supervision tree, or report a definitively idle run",
    )
    status.add_argument("--build-id", default=None)
    status.add_argument("--runtime-root", default=None)

    attach = subparsers.add_parser(
        "attach",
        help="resolve a node through daemon show, then attach to its tmux target",
    )
    attach.add_argument("addr", nargs="?", default=commissioning.L1_ADDRESS)
    attach.add_argument("--build-id", default=None)
    attach.add_argument("--runtime-root", default=None)
    attach.add_argument(
        "--print-only",
        action="store_true",
        help="print the exact tmux argv without attaching",
    )

    # --- Read-only surface (§4.5) ---------------------------------------------------------------
    view = subparsers.add_parser(
        "view",
        help="join the whole live or dead run on read and print terminal or JSON observability",
    )
    view.add_argument("--build-id", default=None)
    view.add_argument("--runtime-root", default=None)
    view.add_argument(
        "--format",
        choices=("terminal", "json"),
        default="terminal",
        help="stdout representation (default: terminal)",
    )

    journey = subparsers.add_parser(
        "journey",
        help="render the self-contained whole-build HTML/SVG journey DAG",
    )
    journey.add_argument("--build-id", default=None)
    journey.add_argument("--runtime-root", default=None)
    journey_output = journey.add_mutually_exclusive_group()
    journey_output.add_argument(
        "--output",
        default=None,
        help="non-node output path (default: <runtime-root>/.harnessd/views/journey.html)",
    )
    journey_output.add_argument(
        "--stdout",
        action="store_true",
        help="emit HTML to stdout and write no file",
    )

    show = subparsers.add_parser("show", help="print the ledger state for a node (read-only)")
    _add_addr(show)

    subparsers.add_parser(
        "tree", help="print the whole supervision tree (address / level / state / liveness; read-only)"
    )

    next_seq = subparsers.add_parser(
        "next-seq", help="print the next monotonic WAL seq (read-only)"
    )

    validate = subparsers.add_parser(
        "validate", help="run the whole-ledger admission scan (read-only)"
    )

    inspect = subparsers.add_parser(
        "reconcile-inspect", help="dry-inspect what a reconcile would do (read-only)"
    )

    # --- Mutating surface (§4.3 — performed BY THE DAEMON, never the CLI) -----------------------
    spawn = subparsers.add_parser("spawn", help="claim-before-spawn a node (mutation -> daemon)")
    _add_addr(spawn)
    spawn.add_argument("--level", default=None, help="the level config (L1..L5) for the spawn seat")
    spawn.add_argument("--expected-state", dest="expected_state", default=None)
    spawn.add_argument("--expected-generation", dest="expected_generation", type=int, default=None)
    spawn.add_argument("--expected-owner-token", dest="expected_owner_token", default=None)
    # The PARENT-SPAWNS-CHILD route (the supervision-tree spawn). When --parent is given the daemon
    # registers the child under the parent + briefs it + spawns it; without it the daemon falls to the
    # EXISTING claim-only spawn of an already-planned node. The CLI only serializes the parent address.
    spawn.add_argument(
        "--parent", dest="parent", default=None,
        help="the parent node address — routes a parent-spawns-child (register+brief+spawn) via the daemon",
    )
    spawn.add_argument(
        "--brief", dest="brief", default=None,
        help="path to a file whose contents are the child's brief (the child's actual task)",
    )

    transition = subparsers.add_parser(
        "transition", help="transition a node to a target state (mutation -> daemon)"
    )
    _add_addr(transition)
    transition.add_argument("--expected-state", dest="expected_state", default=None)
    transition.add_argument("--expected-generation", dest="expected_generation", type=int, default=None)
    transition.add_argument("--expected-owner-token", dest="expected_owner_token", default=None)
    transition.add_argument("--target-state", dest="target_state", required=True)
    transition.add_argument("--event", dest="event", default="transition")

    message = subparsers.add_parser(
        "message",
        help="send one canonical durable message on a direct parent-child edge",
    )
    _add_addr(message)
    message.add_argument("--to", required=True, help="the direct parent or child recipient address")
    message.add_argument("--message-id", dest="message_id", required=True)
    message.add_argument("--summary", default=None)
    message.add_argument("--needs-answer", dest="needs_answer", action="store_true")
    message.add_argument("--tag", dest="tags", action="append", default=[])
    message.add_argument("--answers-asker", dest="answers_asker", default=None)
    message.add_argument("--answers-message-id", dest="answers_message_id", default=None)
    message_src = message.add_mutually_exclusive_group(required=True)
    message_src.add_argument("--text", dest="message_text")
    message_src.add_argument("--file", dest="message_file")

    kill = subparsers.add_parser(
        "kill", help="collapse a node to a terminal state (mutation -> daemon)"
    )
    _add_addr(kill)
    kill.add_argument("--expected-owner-token", dest="expected_owner_token", default=None)
    kill.add_argument(
        "--terminal-signal", dest="terminal_signal", default="FAILED",
        help="the terminal signal (DONE / FAILED / DIED / DEAD); default FAILED",
    )

    # service-outbox: drain a node's spawn-request OUTBOX (FORK-SPAWN-CHANNEL). The daemon adjudicates
    # each request (compose child address from the parent's address + spawn under the parent's token).
    # With --node -> one node's outbox; without -> EVERY live non-leaf node (the operator/loop sweep).
    svc = subparsers.add_parser(
        "service-outbox", help="drain spawn-request outboxes -> register+brief+spawn children (-> daemon)"
    )
    svc.add_argument(
        "--node", dest="addr", default=None,
        help="service ONLY this node's outbox; omit to service every live non-leaf node",
    )

    # The F16 human-control verbs (TRANSPORTS §5.3) — pure arg-parsing here; the DAEMON performs
    # every mutation through the single-writer executor.
    pause = subparsers.add_parser(
        "pause",
        help=(
            "pause a subtree: set paused_at — a FLAG spawner+watchdog respect, NOT a kill; "
            "the in-flight agent keeps running (mutation -> daemon)"
        ),
    )
    _add_addr(pause)

    resume = subparsers.add_parser(
        "resume",
        help="clear paused_at — re-admit children + recovery (mutation -> daemon)",
    )
    _add_addr(resume)

    # Run-level, so deliberately UNADDRESSED: the freeze episode belongs to the run, not a node.
    subparsers.add_parser(
        "escalation-ack",
        help=(
            "acknowledge the daemon's pending run-frozen escalation — closes the episode before "
            "the ladder notifies the user and pauses (-> daemon)"
        ),
    )

    gate_retry = subparsers.add_parser(
        "gate-retry",
        help=(
            "resubmit a gate_failed producer after its review slot has been repaired "
            "(mutation -> daemon)"
        ),
    )
    _add_addr(gate_retry)
    gate_retry.add_argument("--expected-owner-token", dest="expected_owner_token", default=None)

    gate_accept = subparsers.add_parser(
        "gate-accept",
        help=(
            "accept a producer whose review gate escalated to its parent; finalizes the producer "
            "as gate_passed so same-address follow-on work can proceed (mutation -> daemon)"
        ),
    )
    _add_addr(gate_accept)
    gate_accept.add_argument("--resolver", dest="resolver_address", default=None)
    gate_accept.add_argument(
        "--expected-parent-owner-token", dest="expected_parent_owner_token", default=None
    )
    gate_accept.add_argument("--notes", dest="verdict_notes", default=None)

    gate_return = subparsers.add_parser(
        "gate-return",
        help=(
            "return a producer whose review gate escalated to its parent; delivers the parent's "
            "ruling and moves the producer to gate_bounced for a fresh candidate (mutation -> daemon)"
        ),
    )
    _add_addr(gate_return)
    gate_return.add_argument("--resolver", dest="resolver_address", default=None)
    gate_return.add_argument(
        "--expected-parent-owner-token", dest="expected_parent_owner_token", default=None
    )
    gate_return.add_argument("--notes", dest="verdict_notes", required=True)

    test_refresh_approve = subparsers.add_parser(
        "test-refresh-approve",
        help=(
            "approve an L4 post-design acceptance refresh after its L5 test-author child "
            "has passed L5+ review (mutation -> daemon)"
        ),
    )
    _add_addr(test_refresh_approve)
    test_refresh_approve.add_argument("--approver", dest="approver_address", default=None)
    test_refresh_approve.add_argument(
        "--expected-parent-owner-token", dest="expected_parent_owner_token", default=None
    )
    test_refresh_approve.add_argument("--notes", dest="notes", default=None)

    intent_revise = subparsers.add_parser(
        "intent-revise",
        help=(
            "ratify a confirmed L1-owned intent-spec amendment for a direct L2 child "
            "(mutation -> daemon)"
        ),
    )
    _add_addr(intent_revise)
    intent_revise.add_argument("--target", dest="target_address", required=True)
    intent_revise.add_argument("--candidate-ref", dest="candidate_ref", required=True)
    intent_revise.add_argument("--reason", dest="reason", required=True)
    intent_revise.add_argument(
        "--expected-owner-token",
        dest="expected_owner_token",
        required=True,
    )

    answer = subparsers.add_parser(
        "answer",
        help=(
            "answer a current fidelity-playback or plan-alignment owner question, or retain the legacy "
            "human-to-ESCALATED-node behavior (mutation -> daemon)"
        ),
    )
    _add_addr(answer)
    answer.add_argument(
        "--question-id",
        dest="question_id",
        default=None,
        help="current fidelity-playback or plan-alignment owner-question id",
    )
    answer.add_argument(
        "--decision",
        dest="decision",
        choices=("confirm", "reject"),
        default=None,
        help="confirm or reject the exact finding named by --question-id",
    )
    answer.add_argument(
        "--authority",
        dest="answer_authority",
        choices=("owner", "operator-delegate"),
        default=None,
        help=(
            "answer authority; omitted means owner. operator-delegate is valid only when "
            "predeclared for this commissioning run"
        ),
    )
    answer.add_argument(
        "--actor",
        dest="answer_actor",
        default=None,
        help="exact predeclared actor label required with --authority operator-delegate",
    )
    answer_src = answer.add_mutually_exclusive_group(required=True)
    answer_src.add_argument("--text", dest="answer_text", help="the answer text itself")
    answer_src.add_argument(
        "--file", dest="answer_file",
        help="path to a file whose contents are the answer",
    )

    answer_down = subparsers.add_parser(
        "answer-down",
        help=(
            "post a parent decision into an ESCALATED child and wake that child "
            "(mutation -> daemon)"
        ),
    )
    _add_addr(answer_down)
    answer_down_src = answer_down.add_mutually_exclusive_group(required=True)
    answer_down_src.add_argument("--text", dest="answer_text", help="the decision text itself")
    answer_down_src.add_argument(
        "--file", dest="answer_file",
        help="path to a file whose contents are the decision",
    )

    plan_alignment_decision = subparsers.add_parser(
        "plan-alignment-decision",
        help=(
            "post L1's PASS/FAIL plan-alignment gate decision into a running L2 child "
            "and wake that child (mutation -> daemon)"
        ),
    )
    _add_addr(plan_alignment_decision)
    plan_alignment_decision.add_argument(
        "--decision",
        dest="decision",
        choices=("pass", "fail"),
        required=True,
    )
    plan_alignment_src = plan_alignment_decision.add_mutually_exclusive_group(required=True)
    plan_alignment_src.add_argument("--text", dest="decision_text", help="the decision text itself")
    plan_alignment_src.add_argument(
        "--file",
        dest="decision_file",
        help="path to a file whose contents are the decision",
    )

    coordination_decision = subparsers.add_parser(
        "coordination-decision",
        help=(
            "post a parent decision/guidance into a running child's coordination handoff "
            "and wake that child (mutation -> daemon)"
        ),
    )
    _add_addr(coordination_decision)
    coordination_decision.add_argument("--handoff-id", dest="handoff_id", required=True)
    coordination_decision.add_argument(
        "--decision",
        dest="decision",
        choices=("ack", "approve", "reject", "revise", "guidance"),
        required=True,
    )
    coordination_src = coordination_decision.add_mutually_exclusive_group(required=True)
    coordination_src.add_argument("--text", dest="decision_text", help="the decision text itself")
    coordination_src.add_argument(
        "--file",
        dest="decision_file",
        help="path to a file whose contents are the decision",
    )

    coordination_note = subparsers.add_parser(
        "coordination-note",
        help=(
            "post a parent-authored coordination note into a running child and wake that child "
            "(mutation -> daemon)"
        ),
    )
    _add_addr(coordination_note)
    coordination_note.add_argument("--handoff-id", dest="handoff_id", required=True)
    coordination_note.add_argument(
        "--kind",
        dest="handoff_kind",
        choices=(
            "phase_ready",
            "scope_issue",
            "plan_gap",
            "interface_issue",
            "acceptance_gap",
            "approval_request",
            "guidance_request",
            "status_notice",
        ),
        required=True,
    )
    coordination_note.add_argument("--summary", dest="summary", default=None)
    coordination_note_src = coordination_note.add_mutually_exclusive_group(required=True)
    coordination_note_src.add_argument("--text", dest="note_text", help="the note text itself")
    coordination_note_src.add_argument(
        "--file",
        dest="note_file",
        help="path to a file whose contents are the note",
    )

    merge = subparsers.add_parser(
        "merge",
        help=(
            "repair-only merge for a gate-passed source whose automatic merge did not land "
            "(mutation -> daemon)"
        ),
    )
    _add_addr(merge)
    merge.add_argument(
        "--repo", dest="repo_path", required=True,
        help="path to the git work tree where the branch merge should run",
    )
    merge.add_argument(
        "--target-branch", dest="target_branch", default=None,
        help="target branch override; default derives from parent_address or the source branch path",
    )
    merge.add_argument(
        "--requested-by",
        dest="requested_by",
        default=None,
        help=(
            "merge requester for audit evidence; use the parent node address for parent-owned "
            "movement, or omit for operator-owned manual/repair movement"
        ),
    )

    fidelity = subparsers.add_parser(
        "fidelity-playback",
        help=(
            "freeze L1's preliminary fidelity judgment as one owner-confirmable pointer package "
            "(mutation -> daemon)"
        ),
    )
    _add_addr(fidelity)

    # The F8 delivery-terminus caller (INTAKE-TO-DELIVERY Stage 5→6): owner-confirmed accept made
    # concrete. Pure serialization here — the DAEMON performs the cross-jail copy/push and the
    # executor.deliver stamp (promotion stays a harnessd action; the CLI stays a client).
    # --decision is REQUIRED so a fat-fingered bare `promote <addr>` cannot speculatively deliver.
    promote_p = subparsers.add_parser(
        "promote",
        help=(
            "owner-confirmed fidelity playback -> control-plane delivery out of /runtime/ "
            "(mutation -> daemon)"
        ),
    )
    _add_addr(promote_p)
    promote_p.add_argument(
        "--decision", dest="decision", required=True, choices=["accept", "reject"],
        help=(
            "the deliberate delivery trigger; only 'accept' evaluates the owner-final gate — "
            "a reject round-trips as a "
            "recorded no-op (exit 2)"
        ),
    )
    promote_p.add_argument(
        "--acceptance-ref", dest="acceptance_ref", default=None,
        help="the frozen intent-spec the accept was judged against",
    )
    promote_p.add_argument(
        "--delivery-source",
        dest="delivery_source",
        default=argparse.SUPPRESS,
        help=(
            "optional product-surface directory under the node workspace to promote "
            "(for example build/app); default is the whole node workspace"
        ),
    )
    promote_p.add_argument(
        "--delivery-destination",
        dest="delivery_destination_override",
        default=argparse.SUPPRESS,
        help=(
            "explicit retry destination for a previous delivery-failed promote; valid only as a "
            "control-plane repair path after delivery failure"
        ),
    )
    promote_p.add_argument(
        "--delivery-kind",
        dest="delivery_kind_override",
        choices=["filesystem-path", "git-remote", "in-place"],
        default=argparse.SUPPRESS,
        help="delivery kind for --delivery-destination; defaults to the binding/spec kind",
    )
    promote_p.add_argument("--note", dest="note", default=None)

    return parser


# ---------------------------------------------------------------------------
# Request assembly — build the daemon request dict from the parsed namespace. NO mutation here: this is
# pure serialization (the daemon performs the mutation). Only the relevant fields per command are sent.
# ---------------------------------------------------------------------------


# Per-command request fields (besides ``command`` + ``addr``) the client serializes and ships.
_REQUEST_FIELDS = {
    "show": (),
    "next-seq": (),
    "validate": (),
    "reconcile-inspect": (),
    "spawn": ("level", "expected_state", "expected_generation", "expected_owner_token", "parent"),
    "transition": (
        "expected_state",
        "expected_generation",
        "expected_owner_token",
        "target_state",
        "event",
    ),
    "message": (
        "to",
        "message_id",
        "summary",
        "needs_answer",
        "tags",
        "answers_asker",
        "answers_message_id",
    ),
    "kill": ("expected_owner_token", "terminal_signal"),
    "pause": (),
    "resume": (),
    "gate-retry": ("expected_owner_token",),
    "gate-accept": ("resolver_address", "expected_parent_owner_token", "verdict_notes"),
    "gate-return": ("resolver_address", "expected_parent_owner_token", "verdict_notes"),
    "test-refresh-approve": ("approver_address", "expected_parent_owner_token", "notes"),
    "intent-revise": (
        "target_address",
        "candidate_ref",
        "reason",
        "expected_owner_token",
    ),
    "answer": ("question_id", "decision", "answer_authority", "answer_actor"),
    "answer-down": (),
    "plan-alignment-decision": ("decision",),
    "coordination-note": ("handoff_id", "handoff_kind", "summary"),
    "coordination-decision": ("handoff_id", "decision"),
    "merge": ("repo_path", "target_branch", "requested_by"),
    "fidelity-playback": (),
    "promote": (
        "decision",
        "acceptance_ref",
        "delivery_source",
        "delivery_destination_override",
        "delivery_kind_override",
        "note",
    ),
}


def _build_request(args: argparse.Namespace) -> dict:
    """Serialize the parsed namespace into the daemon request dict (pure — no ledger mutation).

    The ONLY non-trivial step is the spawn ``--brief`` flag: the CLIENT reads the brief FILE
    (client-side file I/O, NOT a ledger write — the brief is the child's task text the daemon writes
    into the child node) and ships its CONTENTS as ``brief_content``. The daemon, not the CLI, writes
    the brief into the child node (the cardinal rule: the CLI is a client, never a writer).
    """
    request: dict = {"command": args.command}
    if hasattr(args, "addr"):
        request["addr"] = args.addr
    for field in _REQUEST_FIELDS.get(args.command, ()):  # only the fields this command carries
        if hasattr(args, field):
            value = getattr(args, field)
            if (
                args.command == "answer"
                and field in {
                    "question_id",
                    "decision",
                    "answer_authority",
                    "answer_actor",
                }
                and value is None
            ):
                continue
            request[field] = value
    # spawn --brief <file>: read the file CONTENTS (client-side file read) and ship as brief_content.
    brief_path = getattr(args, "brief", None)
    if args.command == "spawn" and brief_path:
        request["brief_content"] = Path(brief_path).read_text(encoding="utf-8")
    # answer/answer-down --text/--file: ship CONTENTS as answer_content (the --file read mirrors the
    # spawn --brief precedent — client-side file I/O is not ledger I/O; the DAEMON writes/stamps).
    if args.command in {"answer", "answer-down"}:
        if getattr(args, "answer_file", None):
            request["answer_content"] = Path(args.answer_file).read_text(encoding="utf-8")
        else:
            request["answer_content"] = args.answer_text
    if args.command == "message":
        if getattr(args, "message_file", None):
            request["message_content"] = Path(args.message_file).read_text(encoding="utf-8")
        else:
            request["message_content"] = args.message_text
    if args.command in {"plan-alignment-decision", "coordination-decision"}:
        if getattr(args, "decision_file", None):
            request["decision_content"] = Path(args.decision_file).read_text(encoding="utf-8")
        else:
            request["decision_content"] = args.decision_text
    if args.command == "coordination-note":
        if getattr(args, "note_file", None):
            request["note_content"] = Path(args.note_file).read_text(encoding="utf-8")
        else:
            request["note_content"] = args.note_text
    return request


# ---------------------------------------------------------------------------
# The socket round-trip — the CLIENT's ONLY I/O. Connect, send the framed request, read the response to
# EOF, return the parsed JSON. This is socket I/O, NOT ledger I/O: the client never writes the ledger.
# ---------------------------------------------------------------------------


def _resolve_socket_path(explicit: Optional[str], parsed: Optional[str]) -> Optional[str]:
    """Resolve the daemon socket path: explicit kwarg > --socket flag > env var > RUNTIME_ROOT default."""
    if explicit is not None:
        return explicit
    if parsed is not None:
        return parsed
    env_value = os.environ.get(SOCKET_ENV_VAR)
    if env_value:
        return env_value
    if ledger.RUNTIME_ROOT is not None:
        return str(Path(ledger.RUNTIME_ROOT) / ".harnessd" / SOCKET_FILENAME)
    return None


def _round_trip(socket_path: str, request: dict) -> dict:
    """Send ``request`` to the daemon at ``socket_path``, return the parsed JSON response.

    Connects to the AF_UNIX socket, writes the whole request, shuts down the write half (so the daemon's
    EOF-framed read terminates), reads the response to EOF, and parses it. A missing socket / refused
    connection raises (FileNotFoundError / ConnectionError / OSError) — the client is NOT a writer, so
    an unreachable daemon is a hard failure the caller surfaces as a nonzero exit (never a local write).
    """
    payload = json.dumps(request).encode("utf-8")
    with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(payload)
        client.shutdown(socket_mod.SHUT_WR)  # signal EOF so the daemon's read-to-EOF completes
        chunks: list[bytes] = []
        while True:
            data = client.recv(65536)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    if not raw.strip():
        return {"ok": False, "errors": ["empty response from daemon"]}
    return json.loads(raw.decode("utf-8"))


def _wait_for_l1(spec, *, timeout_s: float) -> dict:
    """Wait boundedly for daemon IPC + a nonterminal L1 binding with a real pane target."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    last_error = "daemon did not answer"
    ipc_answered = False
    while True:
        try:
            response = _round_trip(
                str(spec.paths.socket),
                {"command": "show", "addr": commissioning.L1_ADDRESS},
            )
            ipc_answered = True
            binding = response.get("binding") if isinstance(response, dict) else None
            if (
                response.get("ok")
                and isinstance(binding, dict)
                and not states.is_terminal(binding.get("state"))
                and binding.get("tmux_target")
            ):
                return binding
            last_error = "; ".join(response.get("errors") or []) or "L1 not live yet"
        except (ConnectionError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        now = time.monotonic()
        if now >= deadline:
            raise lifecycle.readiness_error(
                spec,
                timeout_s=timeout_s,
                socket_present=ipc_answered,
                last_l1_error=last_error,
            )
        time.sleep(min(0.1, max(0.0, deadline - now)))


def _tmux_attach_command(binding: dict, build_id: str) -> list[str]:
    target = binding.get("tmux_target")
    if not target:
        raise lifecycle.LifecycleError("binding has no tmux_target; cannot attach")
    return [
        "tmux",
        "-L",
        commissioning.tmux_socket_name(build_id),
        "attach-session",
        "-t",
        str(target),
    ]


def _attach_to_binding(binding: dict, *, build_id: str, print_only: bool = False) -> int:
    command = _tmux_attach_command(binding, build_id)
    if print_only:
        print(json.dumps({"ok": True, "command": command, "binding": binding}))
        return 0
    env = dict(os.environ)
    env.pop("TMUX", None)  # attaching to the dedicated server is intentional, never a nested default
    result = subprocess.run(command, env=env, check=False)
    return int(result.returncode)


def _run_start_command(args: argparse.Namespace) -> int:
    try:
        initial_intake = args.initial_intake
        if args.intake_file:
            initial_intake = Path(args.intake_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": "start",
                    "errors": [f"cannot read --intake-file input {args.intake_file!r}: {exc}"],
                }
            )
        )
        return 2

    try:
        review_panel_arms = commissioning.normalize_review_panel_arms(
            args.review_panel_arms
        )
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": "start",
                    "error_code": "review_panel_arm_invalid",
                    "errors": [str(exc)],
                }
            )
        )
        return 2

    try:
        (
            fidelity_playback_authority,
            fidelity_playback_delegate,
            fidelity_playback_delegation_reason,
        ) = commissioning._fidelity_playback_authority()
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": "start",
                    "error_code": "fidelity_playback_declaration_invalid",
                    "errors": [str(exc)],
                }
            )
        )
        return 2

    spec = None
    try:
        runtime_root, build_id = commissioning.resolve_runtime_identity(
            runtime_root=args.runtime_root,
            build_id=args.build_id,
        )
        spec = lifecycle.prepare_run(
            runtime_root=runtime_root,
            build_id=build_id,
            initial_intake=initial_intake,
            fidelity_playback_authority=fidelity_playback_authority,
            fidelity_playback_delegate=fidelity_playback_delegate,
            fidelity_playback_delegation_reason=(
                fidelity_playback_delegation_reason
            ),
            review_panel_arms=review_panel_arms,
        )
        lifecycle.bootstrap_run(spec)
        binding = _wait_for_l1(spec, timeout_s=args.wait_seconds)
    except lifecycle.DaemonAlreadyActive as exc:
        print(json.dumps({"ok": False, "command": "start", "errors": [str(exc)]}))
        return 2
    except lifecycle.LifecycleError as exc:
        payload = {"ok": False, "command": "start", "errors": [str(exc)]}
        if getattr(exc, "error_code", None):
            payload["error_code"] = exc.error_code
        if getattr(exc, "details", None):
            payload["startup"] = exc.details
        if spec is not None:
            payload.update(
                {
                    "label": spec.label,
                    "stdout_log": str(spec.paths.stdout_log),
                    "stderr_log": str(spec.paths.stderr_log),
                }
            )
        print(json.dumps(payload))
        return 3
    finally:
        if spec is not None:
            lifecycle.release_start_guard(spec)

    if args.no_attach:
        print(
            json.dumps(
                {
                    "ok": True,
                    "command": "start",
                    "build_id": build_id,
                    "runtime_root": str(runtime_root),
                    "label": spec.label,
                    "binding": binding,
                }
            )
        )
        return 0
    return _attach_to_binding(binding, build_id=build_id)


def _operator_socket(args, explicit_socket: Optional[str]) -> tuple[str, Path, str]:
    runtime_root, build_id = commissioning.resolve_runtime_identity(
        runtime_root=getattr(args, "runtime_root", None),
        build_id=getattr(args, "build_id", None),
    )
    resolved = _resolve_socket_path(
        explicit_socket,
        getattr(args, "socket_path", None),
    )
    if resolved is None:
        resolved = str(lifecycle.paths_for(runtime_root).socket)
    return str(resolved), runtime_root, build_id


def _run_status(args: argparse.Namespace, *, socket_path: Optional[str]) -> int:
    resolved, runtime_root, build_id = _operator_socket(args, socket_path)
    try:
        response = _round_trip(resolved, {"command": "tree"})
    except (ConnectionError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        paths = lifecycle.paths_for(runtime_root)
        label = lifecycle.launchd_label(runtime_root, build_id)
        protected = lifecycle.instance_lock_is_held(paths.instance_lock)
        if not protected:
            protected = lifecycle.service_is_loaded(label)
        if not protected:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "command": "status",
                        "state": "idle",
                        "build_id": build_id,
                        "runtime_root": str(runtime_root),
                    }
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": "status",
                    "state": "starting-or-recovering",
                    "errors": [f"daemon not yet reachable: {exc}"],
                }
            )
        )
        return 3
    if response.get("ok"):
        print(render_tree(response.get("nodes") or {}))
        return 0
    print(json.dumps(response))
    return 2


def _run_attach(
    args: argparse.Namespace,
    *,
    socket_path: Optional[str],
) -> int:
    resolved, _runtime_root, build_id = _operator_socket(args, socket_path)
    try:
        response = _round_trip(resolved, {"command": "show", "addr": args.addr})
    except (ConnectionError, FileNotFoundError, OSError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": "attach",
                    "errors": [f"daemon unreachable: {exc}"],
                }
            )
        )
        return 3
    if not response.get("ok"):
        print(json.dumps(response))
        return 2
    try:
        return _attach_to_binding(
            response.get("binding") or {},
            build_id=build_id,
            print_only=args.print_only,
        )
    except lifecycle.LifecycleError as exc:
        print(json.dumps({"ok": False, "command": "attach", "errors": [str(exc)]}))
        return 2


def _observability_snapshot(args: argparse.Namespace) -> tuple[dict, Path, str]:
    runtime_root, build_id = commissioning.resolve_runtime_identity(
        runtime_root=getattr(args, "runtime_root", None),
        build_id=getattr(args, "build_id", None),
    )
    return observability.snapshot(runtime_root), runtime_root, build_id


def _run_view(args: argparse.Namespace) -> int:
    try:
        captured, _runtime_root, _build_id = _observability_snapshot(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": "view",
                    "errors": [f"observability join failed: {type(exc).__name__}: {exc}"],
                }
            )
        )
        return 2
    if args.format == "json":
        print(json.dumps(captured, sort_keys=True))
    else:
        print(observability.render_terminal(captured))
    return 0


def _run_journey(args: argparse.Namespace) -> int:
    try:
        captured, runtime_root, build_id = _observability_snapshot(args)
        if args.stdout:
            print(observability.render_html(captured))
            return 0
        output = (
            Path(args.output).expanduser()
            if args.output
            else observability.default_output_path(runtime_root)
        )
        written = observability.write_html(
            captured,
            output,
            runtime_root=runtime_root,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": "journey",
                    "errors": [f"journey render failed: {type(exc).__name__}: {exc}"],
                }
            )
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "command": "journey",
                "build_id": build_id,
                "runtime_root": str(runtime_root),
                "output": str(written),
                "generated_at": captured.get("generated_at"),
            }
        )
    )
    return 0


# ---------------------------------------------------------------------------
# main — parse argv, build the request, ship it over the socket, print the JSON response + return an
# exit code. The ONLY entrypoint. A CLIENT: arg parsing + socket I/O, never a ledger write.
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None, *, socket_path: Optional[str] = None) -> int:
    """Parse ``argv``, ship the request to the daemon, print the JSON response, return an exit code.

    Exit codes (the print-JSON / exit-code convention):
      0 — the daemon reported ``ok`` (a committed mutation or a successful read);
      2 — a command-level abort: the daemon reported ``ok`` false (a CAS miss / illegal edge /
          fencing rejection) OR a client-side input error (an unreadable ``--brief``/``--file``
          input file — the daemon was never contacted, the ledger untouched);
      3 — a transport failure (no daemon reachable, or a garbled non-JSON response) — the client
          cannot perform the mutation itself. Every failure is a printed JSON error line + a nonzero
          exit, never a traceback.

    The mutation path is SOCKET I/O, not ledger I/O: ``main`` builds a request dict and ships it; the
    DAEMON performs the mutation inside the one lock. ``main`` never writes the ledger.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    # Start is the ONE pre-daemon lifecycle exception. It must run before ordinary socket
    # resolution because its job is to create the daemon. Status/attach are named operator helpers:
    # status reuses tree IPC; attach reuses show IPC and then operates tmux locally. View/journey
    # deliberately join explicit runtime paths directly so terminal postmortems work after the
    # run-scoped daemon has exited; both are read-only and never bind the process-global ledger.
    if args.command == "start":
        return _run_start_command(args)
    if args.command == "status":
        return _run_status(args, socket_path=socket_path)
    if args.command == "attach":
        return _run_attach(args, socket_path=socket_path)
    if args.command == "view":
        return _run_view(args)
    if args.command == "journey":
        return _run_journey(args)

    resolved_socket = _resolve_socket_path(socket_path, getattr(args, "socket_path", None))
    if resolved_socket is None:
        print(json.dumps({"ok": False, "errors": ["no daemon socket configured (--socket / $HARNESSD_SOCKET / RUNTIME_ROOT)"]}))
        return 3

    # The client-side input-file reads (spawn --brief / answer --file) get their OWN guard, scoped to
    # the _build_request call only — NOT folded into the transport except below (that would mislabel a
    # missing input file as "daemon unreachable"). The daemon was never contacted -> exit 2 (a
    # command-level abort), the ledger untouched (DAEMON §4.3). OSError covers FileNotFoundError /
    # PermissionError / IsADirectoryError; UnicodeDecodeError covers a binary input file.
    try:
        request = _build_request(args)
    except (OSError, UnicodeDecodeError) as exc:
        # Name the ACTUAL input file: spawn carries --brief, answer/plan-alignment carry --file — report
        # whichever this command shipped, never hard-coding the message to one flag.
        brief_path = getattr(args, "brief", None)
        file_path = getattr(args, "answer_file", None) or getattr(args, "decision_file", None)
        flag, input_path = ("--brief", brief_path) if brief_path else ("--file", file_path)
        print(json.dumps({
            "ok": False,
            "command": args.command,
            "errors": [f"cannot read {flag} input file {input_path!r}: {exc}"],
        }))
        return 2

    try:
        response = _round_trip(resolved_socket, request)
    except (ConnectionError, FileNotFoundError, OSError) as exc:
        # The daemon is unreachable. The CLIENT cannot perform the mutation itself (DAEMON §4.3) — print
        # the error as JSON and fail with a nonzero exit. The ledger is NOT touched.
        print(json.dumps({"ok": False, "command": args.command, "errors": [f"daemon unreachable: {exc}"]}))
        return 3
    except json.JSONDecodeError as exc:
        # The daemon answered with bytes the client cannot parse (json.JSONDecodeError is a ValueError,
        # NOT caught by the OSError-family arm above). The daemon did not speak the protocol — a
        # transport-class failure (exit 3), structured, never a traceback.
        print(json.dumps({
            "ok": False,
            "command": args.command,
            "errors": [f"garbled response from daemon (not JSON): {exc}"],
        }))
        return 3

    # The `tree` read is rendered as a human-readable supervision tree (the operator fleet view, COMP-4);
    # every other command prints the raw JSON response (the machine surface).
    if args.command == "tree" and response.get("ok"):
        print(render_tree(response.get("nodes") or {}))
        return 0
    print(json.dumps(response))
    return 0 if response.get("ok", False) else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
