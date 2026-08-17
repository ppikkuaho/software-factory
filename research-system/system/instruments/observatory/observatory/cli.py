"""ht-observe command-line entry point (C1 — the on-demand per-run pass)."""

from __future__ import annotations

import argparse
import sys

from . import OBSERVATORY_VERSION
from .pipeline import run_observatory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ht-observe",
        description="On-demand observatory pass over one completed L1-L5 run "
                    "(C1 three-layer pipeline: mechanical screens -> triage -> deep dives).",
    )
    parser.add_argument("--version", action="version", version=OBSERVATORY_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the per-run pass over a session bundle")
    run.add_argument("--bundle", required=True,
                     help="path to the completed run's main session .jsonl (READ-ONLY; "
                          "subagents discovered automatically)")
    run.add_argument("--out", default=None,
                     help="output dir (default: readout/observatory/<session-id>/)")
    run.add_argument("--skip-llm", action="store_true",
                     help="run L1 screens only; skip L2 triage + L3 deep dives")
    run.add_argument("--digest", default=None,
                     help="path to the run's Behavioral Record digest for L2 (if omitted, "
                          "one is generated over the trace, counting a claude -p call)")
    run.add_argument("--work-dir", default=None,
                     help="LLM sandbox / extraction dir (default: a temp dir)")
    run.add_argument("--model", default=None, help="model id for the claude -p calls")
    run.add_argument("--stats", default=None,
                     help="statistics store path (default: readout/statistics.json)")
    run.add_argument(
        "--audit-runtime",
        default=None,
        help="read-only harness runtime root for validated caught-at audit events",
    )
    run.add_argument("--k", type=int, default=15,
                     help="first-k actions for the orientation view (default 15)")

    args = parser.parse_args(argv)

    if args.command == "run":
        result = run_observatory(
            args.bundle, out_dir=args.out, work_dir=args.work_dir,
            skip_llm=args.skip_llm, digest_path=args.digest, model=args.model,
            stats_path=args.stats, k=args.k, audit_runtime=args.audit_runtime,
        )
        sys.stdout.write(
            f"observatory pass: {result['session_id']}\n"
            f"  report card     : {result['report_card']}\n"
            f"  screens         : {result['screens_path']}\n"
            f"  statistics      : {result['statistics_path']}\n"
            f"  work dir        : {result['work_dir']}\n"
            f"  spine source    : {result['spine_source']}\n"
            f"  relevant globs  : {result['relevant_globs']}\n"
            f"  generation path : {'mechanical-only (--skip-llm)' if result['skip_llm'] else 'three-layer'}\n"
            f"  claude -p calls : {result['claude_call_count']}"
            f"{' (digest generated)' if result['digest_generated'] else ''}\n"
            f"  above-minor     : {result['targets_above_minor']} flag(s), "
            f"{result['deep_dives']} deep dive(s)\n"
            f"  anchor warnings : {result['anchor_unanchored']} unanchored paragraph(s)\n"
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
