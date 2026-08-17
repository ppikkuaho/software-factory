"""Custody wrapper: started barrier, fixed helper, and exact exit receipt."""

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
import subprocess

from ht.runtime.atomic import publish_immutable, read_exact_file
from ht.runtime.custody import audit_lock, validate_inherited
from ht.runtime.launcher import (
    BARRIER_BYTES,
    child_options,
    helper_argv,
    load_session_context,
)
from ht.runtime.schema import canonical_json_bytes, strict_loads, validate
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
        "wrapper_instance_id": packet["wrapper_instance_id"],
        "helper_instance_id": packet["helper_instance_id"],
    }


def _valid_hash(
    path: Path,
    schema_name: str,
    expected: dict[str, object],
) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    data = read_exact_file(path)
    value = strict_loads(data, label=path.name)
    validate(schema_name, value)
    if any(value.get(key) != item for key, item in expected.items()):
        return None
    return hashlib.sha256(data).hexdigest()


def run(root: Path, session_id: str, custody_fd: int) -> int:
    root = root.resolve()
    estate = root / "var" / "runtime"
    barrier_read = -1
    barrier_write = -1
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        read_state_unlocked(root)
        session, packet, _launch, packet_sha = load_session_context(root, session_id)
        validate_inherited(custody_fd, session / "custody.lock")
        barrier_read, barrier_write = os.pipe()
        try:
            helper = subprocess.Popen(
                helper_argv(root, session_id, custody_fd, barrier_read),
                **child_options(root, (custody_fd, barrier_read)),
            )
            os.close(barrier_read)
            barrier_read = -1
            common = _identity(packet, packet_sha)
            started = {
                "schema_version": "hypothesis-tree-runtime-started/1.0.0",
                **common,
                "wrapper_pid": os.getpid(),
                "helper_pid": helper.pid,
                "started_at": _now(),
            }
            validate("started-receipt.schema.json", started)
            publish_immutable(session / "started.json", canonical_json_bytes(started))
        except Exception:
            if barrier_read >= 0:
                os.close(barrier_read)
            if barrier_write >= 0:
                os.close(barrier_write)
                barrier_write = -1
            raise
    try:
        offset = 0
        while offset < len(BARRIER_BYTES):
            count = os.write(barrier_write, BARRIER_BYTES[offset:])
            if count <= 0:
                raise OSError("short helper barrier write")
            offset += count
    finally:
        os.close(barrier_write)
    wait_status = helper.wait()
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        read_state_unlocked(root)
        session, packet, _launch, packet_sha = load_session_context(root, session_id)
        common = _identity(packet, packet_sha)
        helper_expected = {
            key: value
            for key, value in common.items()
            if key not in {"wrapper_instance_id"}
        }
        result_sha = _valid_hash(session / "result.json", "result.schema.json", helper_expected)
        terminal_sha = _valid_hash(
            session / "terminal.json", "terminal-receipt.schema.json", helper_expected
        )
        process_exit = {
            "schema_version": "hypothesis-tree-runtime-process-exit/1.0.0",
            **common,
            "wrapper_pid": os.getpid(),
            "helper_pid": helper.pid,
            "wait_status": wait_status,
            "result_sha256": result_sha,
            "terminal_sha256": terminal_sha,
            "process_exited_at": _now(),
        }
        validate("process-exit-receipt.schema.json", process_exit)
        publish_immutable(session / "process-exit.json", canonical_json_bytes(process_exit))
    return 0 if wait_status == 0 and result_sha is not None and terminal_sha is not None else 2


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--custody-fd", required=True, type=int)
    args = parser.parse_args()
    try:
        return run(Path(args.root), args.session, args.custody_fd)
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
