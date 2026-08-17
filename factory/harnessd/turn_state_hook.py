"""Executable adapter for Claude command-hook stdin and Codex legacy notify argv JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# This file is invoked by absolute path from a seat whose cwd is its node workspace. Put the
# repository root on sys.path before importing the package; no ambient PYTHONPATH is required.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from harnessd import turn_state  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-turn-state-hook")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--node-address", required=True)
    parser.add_argument("--owner-token", required=True)
    parser.add_argument("--runtime", choices=("claude-code", "codex"), required=True)
    parser.add_argument("payload", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw = args.payload if args.payload is not None else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        turn_state.capture_hook_event(
            runtime_root=args.runtime_root,
            node_address=args.node_address,
            owner_token=args.owner_token,
            runtime=args.runtime,
            payload={
                "type": "malformed_hook_payload",
                "fault_reason": f"invalid JSON: {exc}",
            },
        )
        print(f"invalid hook JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        turn_state.capture_hook_event(
            runtime_root=args.runtime_root,
            node_address=args.node_address,
            owner_token=args.owner_token,
            runtime=args.runtime,
            payload={
                "type": "malformed_hook_payload",
                "fault_reason": f"payload is {type(payload).__name__}, not an object",
            },
        )
        print("invalid hook JSON: payload must be an object", file=sys.stderr)
        return 1
    try:
        response = turn_state.capture_hook_event(
            runtime_root=args.runtime_root,
            node_address=args.node_address,
            owner_token=args.owner_token,
            runtime=args.runtime,
            payload=payload,
        )
    except turn_state.HookResponseTimeout as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if response is not None:
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main/adapter tests
    raise SystemExit(main())
