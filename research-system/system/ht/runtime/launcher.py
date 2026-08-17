"""Frozen claim construction and fixed isolated Python launch vectors."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
from typing import Any

from ht.errors import HtError

from .atomic import read_exact_file, require_directory
from .schema import canonical_json_bytes, strict_loads, validate


WRAPPER_TOKEN = "ht-runtime-wrapper/1.0.0"
HELPER_TOKEN = "ht-runtime-synthetic-helper/1.0.0"
CUSTODY_TOKEN = "inherited-flock-open-description/1.0.0"
BARRIER_TOKEN = "private-pipe-start-token/1.0.0"
BARRIER_BYTES = b"private-pipe-start-token/1.0.0\n"
READINESS_LIMIT = 4096


def construct_claim(
    *,
    runtime_id: str,
    request: dict[str, Any],
    binding_id: str,
    lease_epoch: int,
    session_id: str,
    admission_repository_commit: str,
    wrapper_instance_id: str,
    helper_instance_id: str,
    packet_created_at: str,
) -> tuple[dict[str, Any], bytes, str, dict[str, Any], bytes, str]:
    address = f"runtime/{binding_id}#synthetic"
    packet = {
        "schema_version": "hypothesis-tree-runtime-session-packet/1.0.0",
        "runtime_id": runtime_id,
        "request_id": request["request_id"],
        "binding_id": binding_id,
        "node_address": address,
        "lease_epoch": lease_epoch,
        "session_id": session_id,
        "fence": {
            "binding_id": binding_id,
            "lease_epoch": lease_epoch,
            "session_id": session_id,
        },
        "role": "synthetic-kernel-v1",
        "attempt": request["attempt"],
        "retry_lineage": request["retry_lineage"],
        "work": request["work"],
        "admission_repository_commit": admission_repository_commit,
        "wrapper_instance_id": wrapper_instance_id,
        "helper_instance_id": helper_instance_id,
        "packet_created_at": packet_created_at,
    }
    validate("session-packet.schema.json", packet)
    packet_bytes = canonical_json_bytes(packet)
    packet_sha = hashlib.sha256(packet_bytes).hexdigest()
    launch = {
        "schema_version": "hypothesis-tree-runtime-launch/1.0.0",
        "runtime_id": runtime_id,
        "request_id": request["request_id"],
        "binding_id": binding_id,
        "node_address": address,
        "lease_epoch": lease_epoch,
        "session_id": session_id,
        "role": "synthetic-kernel-v1",
        "wrapper_instance_id": wrapper_instance_id,
        "helper_instance_id": helper_instance_id,
        "packet_relative_path": f"sessions/{session_id}/packet.json",
        "packet_sha256": packet_sha,
        "entrypoint_token": WRAPPER_TOKEN,
        "helper_entrypoint_token": HELPER_TOKEN,
        "custody_protocol": CUSTODY_TOKEN,
        "barrier_protocol": BARRIER_TOKEN,
    }
    validate("launch.schema.json", launch)
    launch_bytes = canonical_json_bytes(launch)
    return (
        packet,
        packet_bytes,
        packet_sha,
        launch,
        launch_bytes,
        hashlib.sha256(launch_bytes).hexdigest(),
    )


def _entry(filename: str) -> Path:
    path = Path(__file__).resolve().with_name(filename)
    if not path.is_file():
        raise HtError(f"trusted runtime entry is absent: {filename} (B1 §13)")
    return path


def _python_entry(filename: str, arguments: list[str]) -> list[str]:
    return [
        # Preserve the absolute virtual-environment interpreter identity.
        # Resolving its symlink would silently discard that environment's
        # installed package set under isolated mode.
        str(Path(sys.executable).absolute()),
        "-I",
        "-X",
        "utf8",
        "-B",
        str(_entry(filename)),
        *arguments,
    ]


def daemon_argv(root: Path, readiness_fd: int) -> list[str]:
    return _python_entry(
        "daemon.py",
        ["--root", str(root.resolve()), "--readiness-fd", str(readiness_fd)],
    )


def wrapper_argv(root: Path, session_id: str, custody_fd: int) -> list[str]:
    return _python_entry(
        "wrapper.py",
        [
            "--root",
            str(root.resolve()),
            "--session",
            session_id,
            "--custody-fd",
            str(custody_fd),
        ],
    )


def helper_argv(root: Path, session_id: str, custody_fd: int, barrier_fd: int) -> list[str]:
    return _python_entry(
        "synthetic_helper.py",
        [
            "--root",
            str(root.resolve()),
            "--session",
            session_id,
            "--custody-fd",
            str(custody_fd),
            "--barrier-fd",
            str(barrier_fd),
        ],
    )


def child_options(root: Path, pass_fds: tuple[int, ...]) -> dict[str, Any]:
    """Return the exact isolated child contract; no ambient environment survives."""

    return {
        "env": {},
        "cwd": str(root.resolve()),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "pass_fds": pass_fds,
        "close_fds": True,
    }


def load_session_context(
    root: Path, session_id: str
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    """Stable-open and cross-check descriptor, packet, launch, path, and tokens."""

    root = root.resolve()
    estate = root / "var" / "runtime"
    session = estate / "sessions" / session_id
    for directory in (estate, estate / "sessions", session):
        require_directory(directory)
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime.json")
    validate("descriptor.schema.json", descriptor)
    packet_bytes = read_exact_file(session / "packet.json")
    packet = strict_loads(packet_bytes, label="packet.json")
    launch = strict_loads(read_exact_file(session / "launch.json"), label="launch.json")
    validate("session-packet.schema.json", packet)
    validate("launch.schema.json", launch)
    packet_sha = hashlib.sha256(packet_bytes).hexdigest()
    shared = (
        "runtime_id",
        "request_id",
        "binding_id",
        "node_address",
        "lease_epoch",
        "session_id",
        "role",
        "wrapper_instance_id",
        "helper_instance_id",
    )
    if any(packet.get(name) != launch.get(name) for name in shared):
        raise HtError("packet and launch identities differ (B1 §12)")
    if (
        descriptor["runtime_id"] != packet["runtime_id"]
        or descriptor["repository_root"] != str(root)
        or packet["session_id"] != session_id
        or packet["fence"] != {
            "binding_id": packet["binding_id"],
            "lease_epoch": packet["lease_epoch"],
            "session_id": packet["session_id"],
        }
        or launch["packet_relative_path"] != f"sessions/{session_id}/packet.json"
        or launch["packet_sha256"] != packet_sha
        or launch["entrypoint_token"] != WRAPPER_TOKEN
        or launch["helper_entrypoint_token"] != HELPER_TOKEN
        or launch["custody_protocol"] != CUSTODY_TOKEN
        or launch["barrier_protocol"] != BARRIER_TOKEN
    ):
        raise HtError("session context differs from fixed launch truth (B1 §12/§13)")
    return session, packet, launch, packet_sha
