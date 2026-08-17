"""Fixed zero-content synthetic helper for the B1 process proof."""

from __future__ import annotations

if __package__ in {None, ""}:  # trusted absolute isolated entry
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

from ht.runtime.atomic import publish_immutable
from ht.runtime.custody import audit_lock, validate_inherited
from ht.runtime.launcher import BARRIER_BYTES, load_session_context
from ht.runtime.schema import canonical_json_bytes, validate
from ht.runtime.views import read_state_unlocked


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity(packet: dict[str, object], packet_sha: str) -> dict[str, object]:
    return {
        "runtime_id": packet["runtime_id"],
        "request_id": packet["request_id"],
        "binding_id": packet["binding_id"],
        "lease_epoch": packet["lease_epoch"],
        "session_id": packet["session_id"],
        "packet_sha256": packet_sha,
        "helper_instance_id": packet["helper_instance_id"],
    }


def run(root: Path, session_id: str, custody_fd: int, barrier_fd: int) -> int:
    root = root.resolve()
    estate = root / "var" / "runtime"
    session = estate / "sessions" / session_id
    # Custody is the only canonical pathname touched before the barrier.  In
    # particular packet/launch/descriptor validation cannot make a helper
    # escape early while the wrapper still holds the barrier writer.
    validate_inherited(custody_fd, session / "custody.lock")
    token = b""
    while len(token) <= len(BARRIER_BYTES):
        chunk = os.read(barrier_fd, len(BARRIER_BYTES) + 1 - len(token))
        if not chunk:
            break
        token += chunk
    os.close(barrier_fd)
    if token != BARRIER_BYTES:
        return 2

    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        read_state_unlocked(root)
        session, packet, _launch, packet_sha = load_session_context(root, session_id)
        validate_inherited(custody_fd, session / "custody.lock")
        common = _identity(packet, packet_sha)
        ready = {
            "schema_version": "hypothesis-tree-runtime-ready/1.0.0",
            **common,
            "helper_pid": os.getpid(),
            "ready_at": _now(),
        }
        validate("ready-receipt.schema.json", ready)
        publish_immutable(session / "ready.json", canonical_json_bytes(ready))

        result = {
            "schema_version": "hypothesis-tree-runtime-result/1.0.0",
            **common,
            "outcome": "SUCCEEDED",
        }
        validate("result.schema.json", result)
        result_bytes = canonical_json_bytes(result)
        publish_immutable(session / "result.json", result_bytes)
        terminal = {
            "schema_version": "hypothesis-tree-runtime-terminal/1.0.0",
            **common,
            "outcome": "SUCCEEDED",
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "terminal_at": _now(),
        }
        validate("terminal-receipt.schema.json", terminal)
        publish_immutable(session / "terminal.json", canonical_json_bytes(terminal))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--custody-fd", required=True, type=int)
    parser.add_argument("--barrier-fd", required=True, type=int)
    args = parser.parse_args()
    try:
        return run(Path(args.root), args.session, args.custody_fd, args.barrier_fd)
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
