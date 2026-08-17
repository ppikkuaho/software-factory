"""ht-cgate command-line entry point."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

from . import ENGINE_VERSION
from .screen import render_screen, run_screen


STANDALONE_DECIDE_ERROR = (
    "decide requires the root ht distribution; ht integration is unavailable"
)
def _root_integration_available() -> bool:
    try:
        return importlib.util.find_spec("ht.cgate") is not None
    except ModuleNotFoundError as exc:
        if exc.name == "ht":
            return False
        raise


def _output_path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    var_dir = (root / "var").resolve()
    if not resolved.is_relative_to(var_dir):
        raise ValueError("--out must resolve under the research root's var/ directory")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ht-cgate",
        description="Deterministic composition-gate instrument over committed state.",
    )
    parser.add_argument("--version", action="version", version=ENGINE_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    screen = sub.add_parser("screen", help="run the five stage-1 mechanical checks")
    screen.add_argument("record_id", metavar="MR-N")
    screen.add_argument("--root", required=True, help="research-root path")
    screen.add_argument("--config", help="screen config path (default: packaged v1)")
    screen.add_argument("--out", help="also write JSON under the research root's var/")
    decide = sub.add_parser("decide", help="prepare a read-only committed decision plan")
    decide.add_argument("record_id", metavar="MR-N")
    decide.add_argument("--root", required=True, help="research-root path")
    decide.add_argument("--execute", action="store_true", help="finalize the decision")
    args = parser.parse_args(argv)

    if args.command == "screen":
        root = Path(args.root).expanduser().resolve()
        result = run_screen(root, args.record_id, config_path=args.config)
        rendered = render_screen(result)
        if args.out:
            try:
                output_path = _output_path(root, args.out)
            except ValueError as exc:
                parser.error(str(exc))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0
    if args.command == "decide":
        if not _root_integration_available():
            parser.error(STANDALONE_DECIDE_ERROR)
        from ht.cgate import (
            DecisionError,
            execute_decision,
            prepare_decision,
            render_decision,
            render_execution,
        )

        try:
            if args.execute:
                rendered = render_execution(
                    execute_decision(args.root, args.record_id)
                )
            else:
                plan = prepare_decision(args.root, args.record_id)
                rendered = render_decision(plan)
        except DecisionError as exc:
            parser.error(f"decision preparation failed [{exc.kind}]: {exc.message}")
        sys.stdout.write(rendered)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
