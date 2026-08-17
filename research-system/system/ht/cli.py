"""ht command-line interface.

Resolves the research root (--root / HT_ROOT / walk-up), reads the acting role from
HT_ROLE (required for mutating commands — DP-A2-4 honor-system at launch, hard
per-field after), dispatches to a command that builds a pipeline.Plan, and runs the
shared enforcement pipeline. Rejections print "REJECTED: <rule> (<note §>)" and exit
non-zero.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import authority, hook, mutex
from .commands import (
    archive,
    claim,
    cursor,
    dispatch,
    issue,
    ledger,
    mrec,
    node,
    observatory,
    interrupt,
    pc,
    pcd,
    pcq,
    phase,
    queue_status,
    report,
    role as role_cmd,
    rq,
    root as root_cmd,
    runtime as runtime_cmd,
    settle as settle_cmd,
    standing,
    tree,
    validate,
)
from .commands._common import Ctx
from .errors import HtError, HtUsageError
from .paths import Root, find_root
from .pipeline import enforce_and_commit

MUTATING_NEEDS_ROLE = True


class _HtArgumentParser(argparse.ArgumentParser):
    """Keep legacy argparse exits, but reserve exit 3 for runtime usage."""

    def error(self, message: str) -> None:
        if (
            self.prog == "ht runtime"
            or self.prog.startswith("ht runtime ")
            or self.prog == "ht role"
            or self.prog.startswith("ht role ")
        ):
            self.print_usage(sys.stderr)
            self.exit(3, f"{self.prog}: error: {message}\n")
        super().error(message)


def _build_parser() -> argparse.ArgumentParser:
    p = _HtArgumentParser(prog="ht", description="hypothesis-tree write tool (v0)")
    p.add_argument("--root", help="research root (default: HT_ROOT or walk up from cwd)")
    sub = p.add_subparsers(dest="group", required=True)

    # Independent hypothesis-tree runtime.  Public mutation identity is minted
    # by the tool, so these commands deliberately do not accept HT role fields.
    g_runtime = sub.add_parser("runtime").add_subparsers(dest="cmd", required=True)
    sp = g_runtime.add_parser("init")
    sp.add_argument("--json", action="store_true")
    sp = g_runtime.add_parser("start")
    sp.add_argument("--background", action="store_true", required=True)
    sp.add_argument("--json", action="store_true")
    sp = g_runtime.add_parser("request")
    sp.add_argument("--work-ref", required=True)
    sp.add_argument("--json", action="store_true")
    sp = g_runtime.add_parser("retry")
    sp.add_argument("request_id", metavar="REQUEST_ID")
    sp.add_argument("--json", action="store_true")
    sp = g_runtime.add_parser("stop")
    sp.add_argument("--json", action="store_true")
    sp = g_runtime.add_parser("status")
    sp.add_argument("--json", action="store_true")
    g_response = g_runtime.add_parser("response").add_subparsers(
        dest="runtime_subcmd", required=True
    )
    sp = g_response.add_parser("show")
    sp.add_argument("--request", required=True)
    sp.add_argument("--json", action="store_true")
    g_packet = g_runtime.add_parser("packet").add_subparsers(
        dest="runtime_subcmd", required=True
    )
    sp = g_packet.add_parser("show")
    sp.add_argument("--session", required=True)
    sp.add_argument("--json", action="store_true")
    sp = g_runtime.add_parser("wait")
    sp.add_argument("request_id", metavar="REQUEST_ID")
    sp.add_argument("--timeout", required=True)
    sp.add_argument("--json", action="store_true")

    # Explicit B1-to-B2 activation.  No other role surface is exposed by the
    # sealed Stage-A contribution.
    g_role = sub.add_parser("role").add_subparsers(dest="cmd", required=True)
    sp = g_role.add_parser("init")
    sp.add_argument("--json", action="store_true", required=True)

    # root init
    g_root = sub.add_parser("root").add_subparsers(dest="cmd", required=True)
    sp = g_root.add_parser("init")
    sp.add_argument("--schemas", help="schema source dir (default: packaged system/schemas)")
    g_root.add_parser("hook-install")

    # tree init
    g_tree = sub.add_parser("tree").add_subparsers(dest="cmd", required=True)
    sp = g_tree.add_parser("init")
    sp.add_argument("component")
    sp.add_argument("--root-question", required=True)

    # node ...
    g_node = sub.add_parser("node").add_subparsers(dest="cmd", required=True)
    sp = g_node.add_parser("mint")
    sp.add_argument("--tree", required=True)
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--parent")
    grp.add_argument("--root", dest="root_flag", action="store_true")
    sp.add_argument("--premise", required=True)
    source = sp.add_mutually_exclusive_group()
    source.add_argument("--minted-from")
    source.add_argument("--from-issue", metavar="I-N")
    sp.add_argument("--supersedes")
    sp.add_argument("--rationale", required=True)
    sp = g_node.add_parser("park")
    sp.add_argument("--node", required=True)
    sp.add_argument("--rationale", required=True)
    sp.add_argument("--superseded-by")
    sp.add_argument("--tree")
    sp = g_node.add_parser("close")
    sp.add_argument("--node", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--refs", nargs="*", default=[])
    sp.add_argument("--tree")
    sp = g_node.add_parser("merge")
    sp.add_argument("--node", required=True)
    sp.add_argument("--tree")
    sp.add_argument("--merge-record", required=True)

    # cursor move
    g_cursor = sub.add_parser("cursor").add_subparsers(dest="cmd", required=True)
    sp = g_cursor.add_parser("move")
    sp.add_argument("--tree", required=True)
    sp.add_argument("--to", required=True)
    sp.add_argument("--rationale", required=True)

    # dispatch ...
    g_dispatch = sub.add_parser("dispatch").add_subparsers(dest="cmd", required=True)
    sp = g_dispatch.add_parser("create")
    sp.add_argument("--node", required=True)
    sp.add_argument("--question", required=True)
    sp.add_argument("--done-definition", required=True)
    sp.add_argument("--plan-ref")
    sp.add_argument("--issue-ref", metavar="I-N")
    sp.add_argument("--tree")
    sp = g_dispatch.add_parser("outcome")
    sp.add_argument("--dispatch", required=True)
    sp.add_argument("--outcome", required=True,
                    choices=["completed", "blocked", "recalled", "retry-operational"])
    sp.add_argument("--tokens", type=int)
    sp.add_argument("--wall-clock", type=float)
    sp.add_argument("--tree")

    # archive write
    g_archive = sub.add_parser("archive").add_subparsers(dest="cmd", required=True)
    sp = g_archive.add_parser("write")
    sp.add_argument("--dispatch", required=True)
    sp.add_argument("--src", required=True)
    sp.add_argument("--name")
    sp.add_argument("--tree")

    # report submit
    g_report = sub.add_parser("report").add_subparsers(dest="cmd", required=True)
    sp = g_report.add_parser("submit")
    sp.add_argument("--dispatch", required=True)
    sp.add_argument("--src", required=True)
    sp.add_argument("--tree")

    # claim grant
    g_claim = sub.add_parser("claim").add_subparsers(dest="cmd", required=True)
    sp = g_claim.add_parser("grant")
    sp.add_argument("--dispatch", required=True)
    sp.add_argument("--text", required=True)
    sp.add_argument("--proposed-tier", type=int, required=True)
    sp.add_argument("--granted-tier", type=int, required=True)
    sp.add_argument(
        "--standing-class", required=True, choices=["trunk", "sandbox", "slice"]
    )
    sp.add_argument("--anchor", action="append", default=[],
                    help="PATH:START:END (repeatable; >=1 enforced by the schema/anchor gate)")
    sp.add_argument("--reason")
    sp.add_argument("--instrument")
    sp.add_argument("--epoch", type=int)
    sp.add_argument("--tree")
    sp = g_claim.add_parser("revalidate")
    sp.add_argument("claim_id")
    sp.add_argument("--ref", required=True)
    sp.add_argument("--epoch", type=int)
    sp.add_argument("--tree")
    sp = g_claim.add_parser("reject")
    sp.add_argument("--dispatch", required=True)
    sp.add_argument("--text", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--tree")

    # standing set
    g_standing = sub.add_parser("standing").add_subparsers(dest="cmd", required=True)
    sp = g_standing.add_parser("set")
    sp.add_argument("--node", required=True)
    sp.add_argument("--standing", required=True)
    sp.add_argument("--note")
    sp.add_argument("--tree")

    # settle
    sp = sub.add_parser("settle")
    sp.add_argument("--node", required=True)
    sp.add_argument("--resolution", required=True, choices=["closed", "revived", "demoted"])
    sp.add_argument("--rationale")
    sp.add_argument("--tree")

    # ledger ...
    g_ledger = sub.add_parser("ledger").add_subparsers(dest="cmd", required=True)
    sp = g_ledger.add_parser(
        "create",
        description=ledger.D8_FRAMING,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument("--section", required=True, choices=["user", "research", "observatory"])
    sp.add_argument(
        "--book",
        help="ledger book (default: verifier HT_LANE; user top)",
    )
    sp.add_argument("--text", required=True)
    provenance = sp.add_mutually_exclusive_group()
    provenance.add_argument("--proposed-by")
    provenance.add_argument(
        "--provenance",
        help="provenance ref (item-2 name; stored in proposed_by for compatibility)",
    )
    sp.add_argument("--from-settlement")
    sp.add_argument("--component")
    disposition = sp.add_mutually_exclusive_group()
    disposition.add_argument(
        "--dedup-distinct",
        nargs="+",
        metavar="L-ID",
        help="explicitly mark every surfaced candidate distinct",
    )
    disposition.add_argument(
        "--abandon-and-echo",
        metavar="L-ID",
        help="abandon this proposal and atomically echo it into a surfaced entry",
    )
    sp = g_ledger.add_parser("status")
    sp.add_argument("--entry", required=True)
    sp.add_argument("--to", required=True, choices=["minted", "retired", "merged-into"])
    sp.add_argument("--ref")
    sp.add_argument("--reason")
    sp = g_ledger.add_parser("echo")
    sp.add_argument("--entry", required=True)
    sp.add_argument("--source-ref", required=True)
    sp.add_argument("--epoch", type=int)
    sp.add_argument("--dedup-matched")
    sp.add_argument("--dedup-resolution", choices=["distinct", "merged"])

    # merge records (composition-gate substrate)
    g_mrec = sub.add_parser("mrec").add_subparsers(dest="cmd", required=True)
    sp = g_mrec.add_parser("create")
    sp.add_argument("--candidate-ref", required=True)
    sp.add_argument("--lane-verdict", required=True)
    sp.add_argument("--lane-adjudication-ref", required=True)
    sp.add_argument("--scope-lane", required=True)
    sp.add_argument("--scope-seat", action="append", default=[])
    sp.add_argument("--scope-surface", action="append", default=[])
    sp.add_argument("--scope-glob", action="append", default=[])
    sp.add_argument(
        "--screen-result",
        action="append",
        default=[],
        metavar="CHECK=RESULT[:DETAIL]",
        help="optional legacy birth result; RESULT is pass, fail, or n/a",
    )
    sp.add_argument("--screen-log-ref")
    sp = g_mrec.add_parser("screen")
    sp.add_argument("record")
    sp.add_argument("--results-json", required=True)
    sp.add_argument("--log-ref", required=True)
    sp = g_mrec.add_parser("verdict")
    sp.add_argument("--record", required=True)
    sp.add_argument("--verdict", required=True)
    sp.add_argument("--note")
    sp = g_mrec.add_parser("list")
    sp.add_argument(
        "--status",
        choices=["pending", "landed", "consumed", "all"],
        default="all",
        help="status filter; landed means gate verdict issued but not consumed",
    )
    sp.add_argument("--last", type=int, metavar="K")
    sp.add_argument("--json", action="store_true")
    sp = g_mrec.add_parser("provenance")
    sp.add_argument("record")
    sp.add_argument("--json", action="store_true")

    # phase set
    g_phase = sub.add_parser("phase").add_subparsers(dest="cmd", required=True)
    sp = g_phase.add_parser("set")
    sp.add_argument("mode", choices=["sign-off", "autonomy"])

    # Tier-1 issue object
    g_issue = sub.add_parser("issue").add_subparsers(dest="cmd", required=True)
    sp = g_issue.add_parser("mint")
    sp.add_argument("--title", required=True)
    sp.add_argument("--question", required=True)
    sp.add_argument("--done-definition", required=True)
    sp.add_argument("--provenance", nargs="+", required=True, metavar="REF")
    sp.add_argument("--lanes", nargs="+", required=True, metavar="LANE")
    for command in ("ratify", "activate", "park", "withdraw"):
        sp = g_issue.add_parser(
            command,
            description=(
                issue.CAPACITY_WARNING_POLICY if command == "activate" else None
            ),
        )
        sp.add_argument("--issue", required=True, metavar="I-N")
    sp = g_issue.add_parser("close")
    sp.add_argument("--issue", required=True, metavar="I-N")
    sp.add_argument("--text", "--closure-text", dest="text", required=True)
    sp.add_argument("--ref", action="append", default=[])
    sp.add_argument("--refs", nargs="*", default=[])
    sp = g_issue.add_parser("subgoal-add")
    sp.add_argument("--issue", required=True, metavar="I-N")
    sp.add_argument("--ref", required=True)
    sp = g_issue.add_parser("observatory-attach")
    sp.add_argument("--issue", required=True, metavar="I-N")
    sp.add_argument("--ref", required=True, metavar="observatory-report#RUN-ID")

    g_observatory = sub.add_parser("observatory").add_subparsers(
        dest="cmd", required=True
    )
    sp = g_observatory.add_parser("register")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--report-card", required=True)

    g_pcq = sub.add_parser("pcq").add_subparsers(dest="cmd", required=True)
    sp = g_pcq.add_parser("set")
    source = sp.add_mutually_exclusive_group(required=True)
    source.add_argument("--src", "--from", dest="src")
    source.add_argument(
        "--entry", action="append", default=[],
        metavar="ISSUE_REF:RANK[:TRIAGE_NOTE]",
    )
    sp.add_argument("--date")
    g_pcq.add_parser("show")

    g_pcd = sub.add_parser("pcd").add_subparsers(dest="cmd", required=True)
    sp = g_pcd.add_parser("append")
    sp.add_argument("--kind", required=True, choices=pcd.KINDS)
    sp.add_argument("--decision", "--text", dest="decision", required=True)
    sp.add_argument("--ref", dest="primary_ref")
    sp.add_argument("--context-ref", action="append", default=[])
    sp.add_argument("--date")

    g_rq = sub.add_parser("rq").add_subparsers(dest="cmd", required=True)
    sp = g_rq.add_parser("append")
    sp.add_argument("--kind", required=True, choices=rq.KINDS)
    sp.add_argument("--payload-ref", required=True)
    sp.add_argument("--text", required=True)
    sp.add_argument("--date")
    sp = g_rq.add_parser("annotate")
    sp.add_argument("--item", required=True, metavar="RQ-N")
    sp.add_argument("--note", required=True)
    sp.add_argument("--date")
    sp = g_rq.add_parser("dispose")
    sp.add_argument("--item", required=True, metavar="RQ-N")
    sp.add_argument(
        "--status", required=True,
        choices=["accepted", "rejected", "deferred"],
    )
    sp.add_argument("--by", required=True)
    sp.add_argument("--note")
    sp.add_argument("--date")
    g_rq.add_parser("list")

    g_interrupt = sub.add_parser("interrupt").add_subparsers(
        dest="cmd", required=True
    )
    for command in ("create", "raise"):
        sp = g_interrupt.add_parser(command)
        sp.add_argument("--raised-by", "--lane", dest="raised_by", required=True)
        sp.add_argument("--issue-ref", required=True, metavar="I-N")
        sp.add_argument("--sub-goal-ref", required=True)
        sp.add_argument("--rationale", required=True)
        sp.add_argument("--date")

    g_pc = sub.add_parser("pc").add_subparsers(dest="cmd", required=True)
    sp = g_pc.add_parser("spawn")
    mode = sp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--launch", action="store_true")
    sp.add_argument("--out")
    sp.add_argument("--decision-tail", type=int, default=10)

    # read-only tier-1 ratification queue status / daily notification
    sp = sub.add_parser("queue-status")
    sp.add_argument(
        "--notify",
        action="store_true",
        help="post a macOS notification when pending items exist",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="with --notify, print the would-be notification without osascript",
    )

    # validate
    sp = sub.add_parser("validate")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--tree")

    # hidden pre-commit shim
    sub.add_parser("_precommit")

    return p


def _require_role() -> tuple[str, object | str]:
    role = os.environ.get("HT_ROLE")
    if not role:
        raise HtUsageError(
            "HT_ROLE is required for mutating commands "
            "(director|verifier|unit|user|harness|pc|cgate) (DP-A2-4)"
        )
    raw_lane = os.environ.get("HT_LANE", "")
    lane = (
        raw_lane.strip()
        if role == "verifier" and raw_lane.strip()
        else authority.UNASSIGNED_LANE
    )
    return role, lane


def _run_plan(root_obj: Root, plan, lane) -> int:
    enforce_and_commit(root_obj, plan, lane=lane)
    for warning in plan.warnings:
        sys.stderr.write(f"WARNING: {warning}\n")
    return 0


def _needs_global_mutex(args: argparse.Namespace) -> bool:
    """Whether plan construction includes a globally serialized operation.

    The lock starts before plan construction: ledger creation allocates its ID
    while building the plan, so locking only around the commit would leave the
    named cross-book next-ID race open (item 1 W2/W7).
    """
    group = args.group
    command = getattr(args, "cmd", None)
    return (group == "ledger" and command == "create") or (
        group == "node" and command == "merge"
    ) or (group == "mrec" and command in {"create", "screen"}) or (
        group == "issue" and command == "mint"
    ) or (group == "observatory" and command == "register") or (
        group == "issue" and command == "observatory-attach"
    ) or (group == "pcd" and command == "append") or (
        group == "rq" and command == "append"
    ) or (group == "interrupt" and command in {"create", "raise"})


def _dispatch(args: argparse.Namespace) -> int:
    # read-only / bootstrap commands that resolve the root differently
    if args.group == "_precommit":
        return hook.run()

    if args.group == "root" and args.cmd == "init":
        _require_role()  # honor-system; root init stamps harness internally
        # a fresh sandbox root need not pre-exist; scaffold at --root/HT_ROOT/cwd
        root_obj = Root(_new_root(args.root))
        root_cmd.run(root_obj, args.schemas)
        return 0

    root_obj = Root(find_root(args.root))

    if args.group == "root" and args.cmd == "hook-install":
        _require_role()
        root_cmd.install_hook(root_obj)
        return 0

    if args.group == "role":
        if args.cmd == "init":
            with mutex.global_mutex(root_obj):
                return role_cmd.init(root_obj, as_json=args.json)
        raise HtUsageError("unknown role command")

    if args.group == "runtime":
        if args.cmd == "init":
            with mutex.global_mutex(root_obj):
                return runtime_cmd.init(root_obj, as_json=args.json)
        if args.cmd == "start":
            return runtime_cmd.start(
                root_obj, background=args.background, as_json=args.json
            )
        if args.cmd == "request":
            return runtime_cmd.request(root_obj, args.work_ref, as_json=args.json)
        if args.cmd == "retry":
            return runtime_cmd.retry(root_obj, args.request_id, as_json=args.json)
        if args.cmd == "stop":
            return runtime_cmd.stop(root_obj, as_json=args.json)
        if args.cmd == "status":
            return runtime_cmd.status(root_obj, as_json=args.json)
        if args.cmd == "response" and args.runtime_subcmd == "show":
            return runtime_cmd.response_show(
                root_obj, args.request, as_json=args.json
            )
        if args.cmd == "packet" and args.runtime_subcmd == "show":
            return runtime_cmd.packet_show(
                root_obj, args.session, as_json=args.json
            )
        if args.cmd == "wait":
            return runtime_cmd.wait(
                root_obj, args.request_id, args.timeout, as_json=args.json
            )
        raise HtUsageError("unknown runtime command")

    if args.group == "mrec" and args.cmd == "list":
        return mrec.list_records(
            root_obj,
            status=args.status,
            last=args.last,
            as_json=args.json,
        )
    if args.group == "mrec" and args.cmd == "provenance":
        return mrec.provenance(root_obj, args.record, as_json=args.json)

    if args.group == "queue-status":
        if args.dry_run and not args.notify:
            raise HtUsageError("--dry-run requires --notify")
        return queue_status.run(root_obj, notify=args.notify, dry_run=args.dry_run)

    if args.group == "pcq" and args.cmd == "show":
        return pcq.show(root_obj)
    if args.group == "rq" and args.cmd == "list":
        return rq.list_items(root_obj)
    if args.group == "pc" and args.cmd == "spawn":
        return pc.spawn(
            root_obj,
            dry_run=args.dry_run,
            launch=args.launch,
            out=args.out,
            decision_tail=args.decision_tail,
        )

    if args.group == "validate":
        ctx = Ctx(root_obj, os.environ.get("HT_ROLE", "harness"))
        errors = validate.run(ctx, args.all, args.tree)
        for warning in validate.join_asymmetry_warnings(ctx, args.all, args.tree):
            sys.stderr.write(f"WARNING: {warning}\n")
        for info in validate.lca_rehoming_debt_inventory(
            ctx, args.all, args.tree
        ):
            print(info)
        if errors:
            for e in errors:
                sys.stderr.write(f"INVALID: {e}\n")
            return 2
        print("OK: all state files schema-valid")
        return 0

    # mutating commands
    role, lane = _require_role()
    ctx = Ctx(root_obj, role, lane)
    if _needs_global_mutex(args):
        with mutex.global_mutex(root_obj):
            plan = _build_plan(ctx, args)
            return _run_plan(root_obj, plan, lane)
    plan = _build_plan(ctx, args)
    return _run_plan(root_obj, plan, lane)


def _new_root(explicit: str | None):
    from pathlib import Path
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("HT_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def _build_plan(ctx: Ctx, args: argparse.Namespace):
    g, c = args.group, getattr(args, "cmd", None)
    if g == "tree" and c == "init":
        return tree.init(ctx, args.component, args.root_question)
    if g == "node" and c == "mint":
        parent = None if args.root_flag else args.parent
        return node.mint(ctx, args.tree, parent, args.root_flag, args.premise,
                         args.minted_from, args.supersedes, args.rationale,
                         args.from_issue)
    if g == "node" and c == "park":
        return node.park(ctx, args.node, args.rationale, args.superseded_by, args.tree)
    if g == "node" and c == "close":
        return node.close(ctx, args.node, args.reason, args.refs, args.tree)
    if g == "node" and c == "merge":
        return node.merge(ctx, args.node, args.tree, args.merge_record)
    if g == "cursor" and c == "move":
        return cursor.move(ctx, args.tree, args.to, args.rationale)
    if g == "dispatch" and c == "create":
        return dispatch.create(ctx, args.node, args.question, args.done_definition,
                               args.plan_ref, args.tree, args.issue_ref)
    if g == "dispatch" and c == "outcome":
        return dispatch.outcome(ctx, args.dispatch, args.outcome, args.tokens,
                                args.wall_clock, args.tree)
    if g == "archive" and c == "write":
        return archive.write(ctx, args.dispatch, args.src, args.name, args.tree)
    if g == "report" and c == "submit":
        return report.submit(ctx, args.dispatch, args.src, args.tree)
    if g == "claim" and c == "grant":
        return claim.grant(ctx, args.dispatch, args.text, args.proposed_tier,
                           args.granted_tier, args.standing_class, args.anchor,
                           args.reason, args.instrument, args.epoch, args.tree)
    if g == "claim" and c == "revalidate":
        return claim.revalidate(ctx, args.claim_id, args.ref, args.epoch, args.tree)
    if g == "claim" and c == "reject":
        return claim.reject(ctx, args.dispatch, args.text, args.reason, args.tree)
    if g == "standing" and c == "set":
        return standing.set_standing(ctx, args.node, args.standing, args.note, args.tree)
    if g == "settle":
        return settle_cmd.settle(ctx, args.node, args.resolution, args.rationale, args.tree)
    if g == "ledger" and c == "create":
        book = args.book.strip() if args.book and args.book.strip() else None
        if book is None:
            if ctx.role == "verifier" and isinstance(ctx.lane, str):
                book = ctx.lane.strip() or None
            elif ctx.role == "user":
                book = "top"
        if book is None:
            # Non-verifier/user invocations will be rejected by section authority,
            # but choose a deterministic path so no implicit lane is invented.
            book = "top"
        provenance = args.provenance or args.proposed_by
        return ledger.create(ctx, args.section, args.text, provenance,
                             args.from_settlement, args.component, book,
                             args.dedup_distinct, args.abandon_and_echo)
    if g == "ledger" and c == "status":
        return ledger.status(ctx, args.entry, args.to, args.ref, args.reason)
    if g == "ledger" and c == "echo":
        return ledger.echo(
            ctx,
            args.entry,
            args.source_ref,
            args.epoch,
            args.dedup_matched,
            args.dedup_resolution,
        )
    if g == "mrec" and c == "create":
        return mrec.create(
            ctx,
            args.candidate_ref,
            args.lane_verdict,
            args.lane_adjudication_ref,
            args.scope_lane,
            args.scope_seat,
            args.scope_surface,
            args.scope_glob,
            [_parse_screen_result(value) for value in args.screen_result],
            args.screen_log_ref,
        )
    if g == "mrec" and c == "screen":
        return mrec.screen(ctx, args.record, args.results_json, args.log_ref)
    if g == "mrec" and c == "verdict":
        return mrec.verdict(ctx, args.record, args.verdict, args.note)
    if g == "phase" and c == "set":
        return phase.set_mode(ctx, args.mode)
    if g == "issue" and c == "mint":
        return issue.mint(
            ctx,
            args.title,
            args.question,
            args.done_definition,
            args.provenance,
            args.lanes,
        )
    if g == "issue" and c == "ratify":
        return issue.ratify(ctx, args.issue)
    if g == "issue" and c == "activate":
        return issue.activate(ctx, args.issue)
    if g == "issue" and c == "park":
        return issue.park(ctx, args.issue)
    if g == "issue" and c == "withdraw":
        return issue.withdraw(ctx, args.issue)
    if g == "issue" and c == "close":
        return issue.close(ctx, args.issue, args.text, args.ref + args.refs)
    if g == "issue" and c == "subgoal-add":
        return issue.subgoal_add(ctx, args.issue, args.ref)
    if g == "issue" and c == "observatory-attach":
        return issue.observatory_attach(ctx, args.issue, args.ref)
    if g == "observatory" and c == "register":
        return observatory.register(ctx, args.run_id, args.report_card)
    if g == "pcq" and c == "set":
        return pcq.set_queue(ctx, src=args.src, entries=args.entry, date=args.date)
    if g == "pcd" and c == "append":
        return pcd.append(
            ctx,
            kind=args.kind,
            decision=args.decision,
            refs=args.context_ref,
            primary_ref=args.primary_ref,
            date=args.date,
        )
    if g == "rq" and c == "append":
        return rq.append(
            ctx,
            kind=args.kind,
            payload_ref=args.payload_ref,
            text=args.text,
            date=args.date,
        )
    if g == "rq" and c == "annotate":
        return rq.annotate(ctx, item_id=args.item, note=args.note, date=args.date)
    if g == "rq" and c == "dispose":
        return rq.dispose(
            ctx,
            item_id=args.item,
            status=args.status,
            by=args.by,
            note=args.note,
            date=args.date,
        )
    if g == "interrupt" and c in {"create", "raise"}:
        return interrupt.create(
            ctx,
            raised_by=args.raised_by,
            issue_ref=args.issue_ref,
            sub_goal_ref=args.sub_goal_ref,
            rationale=args.rationale,
            date=args.date,
        )
    raise HtUsageError(f"unknown command: {g} {c}")


def _parse_screen_result(value: str) -> dict:
    """Parse CHECK=RESULT[:DETAIL] without constraining check/detail vocabulary."""
    if "=" not in value:
        raise HtUsageError(
            "--screen-result must be CHECK=RESULT[:DETAIL] (item 1 W6)"
        )
    check, result_and_detail = value.split("=", 1)
    result, separator, detail = result_and_detail.partition(":")
    parsed = {"check": check, "result": result}
    if separator:
        parsed["detail"] = detail
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except HtError as exc:
        sys.stderr.write(f"REJECTED: {exc.message}\n")
        return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
