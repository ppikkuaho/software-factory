"""ht-trace command-line entry point."""

from __future__ import annotations

import argparse
import sys

from . import EXTRACTOR_VERSION
from .digests import build_digest, restamp_digest
from .extract import extract
from .orient import orient
from .overview import build_overview


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ht-trace",
        description="Deterministic trace reader for Claude Code bundles and Codex rollouts.",
    )
    parser.add_argument("--version", action="version", version=EXTRACTOR_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("extract", help="extract a session bundle into a trace output dir")
    ex.add_argument("--bundle", required=True,
                    help="path to the main session .jsonl (subagents discovered automatically)")
    ex.add_argument("--out", required=True, help="output directory (created if absent)")
    ex.add_argument("--format", default="auto", choices=["auto", "claude", "codex"],
                    help="source format (default: auto-detect; recorded in meta.json)")

    ori = sub.add_parser("orient", help="compute orientation features over an extracted trace")
    ori.add_argument("--trace", required=True, help="an extracted trace dir (holds trace.jsonl)")
    ori.add_argument("--relevant", required=True, action="append", metavar="GLOB",
                     help="task-relevant path glob (fnmatch; repeatable)")
    ori.add_argument("--k", type=int, default=15, help="first-k actions to summarize (default 15)")

    dg = sub.add_parser("digest", help="LLM Behavioral-Record digest over an extracted trace")
    dg_source = dg.add_mutually_exclusive_group(required=True)
    dg_source.add_argument("--trace", help="an extracted trace dir (holds trace.jsonl)")
    dg_source.add_argument(
        "--restamp",
        metavar="DIGEST_FILE",
        help="recompute only anchor accounting/warnings on an existing digest",
    )
    dg.add_argument("--flavor", default="record", choices=["record"],
                    help="digest flavor (default: record)")
    dg.add_argument("--model", default=None, help="model id for claude -p (default: its default)")

    ov = sub.add_parser("overview", help="mechanical live-read Progress Overview (control signal)")
    ov.add_argument("--bundle", required=True, help="path to the main session .jsonl")
    ov.add_argument("--stale-minutes", type=int, default=30,
                    help="staleness threshold for the run-state label (default 30)")
    ov.add_argument("--out", default=None, help="also write the overview to this file")

    args = parser.parse_args(argv)

    if args.command == "extract":
        meta = extract(args.bundle, args.out, format=args.format)
        counts = meta["counts"]
        sys.stdout.write(
            f"extracted {args.bundle}\n"
            f"  active messages : {counts['active_path_messages']}\n"
            f"  abandoned msgs  : {counts['abandoned_messages']}\n"
            f"  branch groups   : {counts['branch_groups']}\n"
            f"  compact bounds  : {counts['compact_boundaries']}\n"
            f"  subagent joins  : {counts['subagent_joins']}\n"
            f"  events (active) : {counts['events_active']}\n"
            f"  events (branch) : {counts['events_branches']}\n"
            f"  format          : {meta.get('format', {}).get('detected')}\n"
            f"  partial         : {meta['partial']}\n"
        )
        return 0

    if args.command == "orient":
        result = orient(args.trace, args.relevant, k=args.k)
        sys.stdout.write(f"oriented {args.trace}  (relevant: {args.relevant}, k={args.k})\n")
        for actor, a in result["actors"].items():
            frr = a["first_relevant_read"]
            frr_s = (f"step {frr['step']} ({frr['latency_ms']} ms, {frr['path']})"
                     if frr else "NONE")
            sys.stdout.write(
                f"  {actor}: reads={a['read_count']}  "
                f"first_relevant={frr_s}  "
                f"before={a['reads_before_first_relevant']['count']}  "
                f"off_task={a['off_task_read_count']}\n"
            )
        return 0

    if args.command == "digest":
        if args.restamp:
            result = restamp_digest(args.restamp)
            check = result["anchor_check"]
            sys.stdout.write(
                f"digest restamped: {result['path']}\n"
                f"  paragraphs_checked : {check['paragraphs_checked']}\n"
                f"  anchored           : {check['anchored']}\n"
                f"  exempted           : {check['exempted']}\n"
                f"  unanchored         : {check['unanchored']}\n"
                f"  body_sha256        : {result['body_sha256']}\n"
            )
            return 0
        result = build_digest(args.trace, flavor=args.flavor, model=args.model)
        h = result["header"]
        sys.stdout.write(
            f"digest written: {result['path']}\n"
            f"  generation_path : {h['generation_path']}\n"
            f"  model           : {h['model']}\n"
            f"  template        : {h['prompt_template']['name']} "
            f"(sha256 {h['prompt_template']['sha256'][:12]})\n"
            f"  generated_at    : {h['generated_at']}\n"
            f"  anchor_check    : {h['anchor_check']['paragraphs_checked']} checked, "
            f"{h['anchor_check']['anchored']} anchored, "
            f"{h['anchor_check']['exempted']} exempted, "
            f"{h['anchor_check']['unanchored']} unanchored\n"
            f"  orphan_events   : {h['orphan_events']}\n"
        )
        if result["orphan_warning"]:
            sys.stdout.write(f"  WARN: {result['orphan_warning']}\n")
        return 0

    if args.command == "overview":
        md = build_overview(args.bundle, stale_minutes=args.stale_minutes)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(md)
        sys.stdout.write(md)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
