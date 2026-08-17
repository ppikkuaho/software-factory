"""Initial public acceptance contract for the independent B1 runtime kernel."""

from __future__ import annotations

import hashlib
from importlib import resources
import json
import stat
from pathlib import Path
from uuid import UUID

from conftest import Sandbox


RUNTIME_MODULES = {
    "__init__.py",
    "atomic.py",
    "schema.py",
    "repository.py",
    "wal.py",
    "replay.py",
    "state.py",
    "custody.py",
    "launcher.py",
    "daemon.py",
    "wrapper.py",
    "synthetic_helper.py",
    "views.py",
}

RUNTIME_SCHEMAS = {
    "descriptor.schema.json",
    "checkpoint.schema.json",
    "request.schema.json",
    "admission-response.schema.json",
    "control-request.schema.json",
    "control-response.schema.json",
    "session-packet.schema.json",
    "launch.schema.json",
    "started-receipt.schema.json",
    "ready-receipt.schema.json",
    "result.schema.json",
    "terminal-receipt.schema.json",
    "process-exit-receipt.schema.json",
    "packet-audit-envelope.schema.json",
}

RUNTIME_HELP_SURFACES = (
    (
        ("runtime", "--help"),
        ("init", "start", "request", "retry", "stop", "status", "response", "packet", "wait"),
    ),
    (("runtime", "init", "--help"), ("--json",)),
    (("runtime", "start", "--help"), ("--background", "--json")),
    (("runtime", "request", "--help"), ("--work-ref", "--json")),
    (("runtime", "retry", "--help"), ("REQUEST_ID", "--json")),
    (("runtime", "stop", "--help"), ("--json",)),
    (("runtime", "status", "--help"), ("--json",)),
    (("runtime", "response", "show", "--help"), ("--request", "--json")),
    (("runtime", "packet", "show", "--help"), ("--session", "--json")),
    (("runtime", "wait", "--help"), ("REQUEST_ID", "--timeout", "--json")),
)

GENESIS_INVENTORY = {
    ".harnessd.lock": "file",
    ".ht-runtime.instance.lock": "file",
    "binding-ledger.json": "file",
    "checkpoint.json": "file",
    "control": "directory",
    "control/requests": "directory",
    "control/responses": "directory",
    "requests": "directory",
    "responses": "directory",
    "run-ledger.jsonl": "file",
    "runtime.json": "file",
    "sessions": "directory",
}


def _successful_json(result, operation: str) -> dict:
    assert result.returncode == 0, (
        f"{operation} failed with exit {result.returncode}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{operation} did not return strict JSON") from exc
    assert isinstance(value, dict), f"{operation} must return one JSON object"
    return value


def _canonical_uuid(value: object) -> str:
    assert isinstance(value, str)
    parsed = UUID(value)
    assert str(parsed) == value
    return value


def _sha256(value: object) -> str:
    assert isinstance(value, str)
    assert len(value) == 64 and value == value.lower()
    int(value, 16)
    return value


def _runtime_inventory(root: Path) -> dict[str, str]:
    inventory = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            kind = "file"
        elif stat.S_ISDIR(mode):
            kind = "directory"
        elif stat.S_ISLNK(mode):
            kind = "symlink"
        else:
            kind = "other"
        inventory[relative] = kind
    return dict(sorted(inventory.items()))


def _runtime_snapshot(root: Path) -> tuple:
    rows = []
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        payload = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        rows.append((relative, info.st_mode, info.st_mtime_ns, payload))
    return tuple(rows)


def _contains(value: object, expected: object) -> bool:
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, expected) for item in value)
    return False


def _mint_synthetic_issue(sandbox: Sandbox) -> None:
    result = sandbox.run(
        "issue",
        "mint",
        "--title",
        "SYNTHETIC-B1-TOKEN-0",
        "--question",
        "SYNTHETIC-B1-TOKEN-1",
        "--done-definition",
        "SYNTHETIC-B1-TOKEN-2",
        "--provenance",
        "user-seed#synthetic-b1-0",
        "--lanes",
        "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr


def test_runtime_modules_and_schemas_are_packaged() -> None:
    package = resources.files("ht")
    runtime = package.joinpath("runtime")
    missing_modules = sorted(
        name for name in RUNTIME_MODULES if not runtime.joinpath(name).is_file()
    )
    schema_root = runtime.joinpath("schemas")
    missing_schemas = sorted(
        name for name in RUNTIME_SCHEMAS if not schema_root.joinpath(name).is_file()
    )

    assert not missing_modules, f"missing packaged B1 runtime modules: {missing_modules}"
    assert not missing_schemas, f"missing packaged B1 runtime schemas: {missing_schemas}"


def test_runtime_public_cli_surface_is_discoverable(sandbox: Sandbox) -> None:
    for argv, required_tokens in RUNTIME_HELP_SURFACES:
        result = sandbox.run(*argv)
        assert result.returncode == 0, (
            f"ht {' '.join(argv)} was not discoverable: {result.stderr!r}"
        )
        help_text = result.stdout + result.stderr
        for token in required_tokens:
            assert token in help_text, f"ht {' '.join(argv)} omitted {token}"


def test_runtime_init_creates_exact_idempotent_genesis(sandbox: Sandbox) -> None:
    first = sandbox.run("runtime", "init", "--json")
    _successful_json(first, "runtime init")

    runtime = sandbox.root / "var/runtime"
    assert _runtime_inventory(runtime) == GENESIS_INVENTORY
    assert stat.S_IMODE(runtime.lstat().st_mode) == 0o700
    for relative, kind in GENESIS_INVENTORY.items():
        mode = stat.S_IMODE((runtime / relative).lstat().st_mode)
        assert mode == (0o700 if kind == "directory" else 0o600)

    descriptor = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
    assert set(descriptor) == {
        "schema_version",
        "runtime_kind",
        "build_id",
        "runtime_id",
        "runtime_root",
        "repository_root",
        "created_at",
    }
    assert descriptor["schema_version"] == "hypothesis-tree-runtime/1.0.0"
    assert descriptor["runtime_kind"] == "hypothesis-tree"
    assert descriptor["build_id"] == "ht-runtime-kernel/1.0.0"
    _canonical_uuid(descriptor["runtime_id"])
    assert descriptor["runtime_root"] == str(runtime.resolve())
    assert descriptor["repository_root"] == str(sandbox.root.resolve())
    assert isinstance(descriptor["created_at"], str) and descriptor["created_at"]

    assert (runtime / "run-ledger.jsonl").read_bytes() == b""
    bindings = json.loads(
        (runtime / "binding-ledger.json").read_text(encoding="utf-8")
    )
    assert list(bindings) == ["runtime#kernel"]
    assert bindings["runtime#kernel"]["node_address"] == "runtime#kernel"
    checkpoint = json.loads((runtime / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["last_seq"] == 0
    for name in ("request_index", "dedup_index", "session_index", "control_index"):
        assert checkpoint[name] == {}

    before = _runtime_snapshot(runtime)
    second = sandbox.run("runtime", "init", "--json")
    _successful_json(second, "idempotent runtime init")
    assert _runtime_snapshot(runtime) == before


def test_runtime_typed_request_reaches_stored_terminal_success(
    sandbox: Sandbox,
) -> None:
    _mint_synthetic_issue(sandbox)
    _successful_json(
        sandbox.run("runtime", "init", "--json"),
        "runtime init",
    )

    start = sandbox.run("runtime", "start", "--background", "--json")
    started = start.returncode == 0
    stopped = False
    try:
        start_payload = _successful_json(start, "runtime start")
        assert start_payload["status"] == "ready"
        _canonical_uuid(start_payload["daemon_incarnation_id"])
        assert isinstance(start_payload["pid"], int) and not isinstance(
            start_payload["pid"], bool
        )

        request_payload = _successful_json(
            sandbox.run(
                "runtime",
                "request",
                "--work-ref",
                "issue#I-1",
                "--json",
            ),
            "runtime request",
        )
        request_id = _canonical_uuid(request_payload["request_id"])
        _sha256(request_payload["request_sha256"])

        waited = _successful_json(
            sandbox.run(
                "runtime",
                "wait",
                request_id,
                "--timeout",
                "30",
                "--json",
            ),
            "runtime wait",
        )
        assert _contains(waited, request_id)
        assert _contains(waited, "SUCCEEDED")

        response_result = sandbox.run(
            "runtime",
            "response",
            "show",
            "--request",
            request_id,
            "--json",
        )
        response = _successful_json(response_result, "runtime response show")
        assert all(not isinstance(value, (dict, list)) for value in response.values())
        assert response["status"] == "accepted"
        assert response["request_id"] == request_id
        binding_id = _canonical_uuid(response["binding_id"])
        session_id = _canonical_uuid(response["session_id"])
        assert response["node_address"] == f"runtime/{binding_id}#synthetic"
        assert isinstance(response["lease_epoch"], int) and not isinstance(
            response["lease_epoch"], bool
        )
        packet_sha256 = _sha256(response["packet_sha256"])

        session = sandbox.root / "var/runtime/sessions" / session_id
        packet_path = session / "packet.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        assert hashlib.sha256(packet_path.read_bytes()).hexdigest() == packet_sha256
        assert packet["request_id"] == request_id
        assert packet["binding_id"] == binding_id
        assert packet["session_id"] == session_id
        assert packet["lease_epoch"] == response["lease_epoch"]
        assert packet["role"] == "synthetic-kernel-v1"

        result = json.loads((session / "result.json").read_text(encoding="utf-8"))
        terminal = json.loads(
            (session / "terminal.json").read_text(encoding="utf-8")
        )
        for artifact in (result, terminal):
            assert artifact["session_id"] == session_id
            assert artifact["packet_sha256"] == packet_sha256
        assert terminal["outcome"] == "SUCCEEDED"

        packet_view = _successful_json(
            sandbox.run(
                "runtime",
                "packet",
                "show",
                "--session",
                session_id,
                "--json",
            ),
            "runtime packet show",
        )
        assert packet_view["packet"] == packet
        assert packet_view["packet_sha256"] == packet_sha256
        assert packet_view["outcome"] == "SUCCEEDED"

        status = _successful_json(
            sandbox.run("runtime", "status", "--json"),
            "runtime status",
        )
        assert _contains(status, request_id)
        assert _contains(status, session_id)
        assert _contains(status, "SUCCEEDED")

        response_path = sandbox.root / f"var/runtime/responses/{request_id}.json"
        response_bytes = response_path.read_bytes()
        requests_before = sorted(
            path.name for path in (sandbox.root / "var/runtime/requests").iterdir()
        )
        retry = sandbox.run("runtime", "retry", request_id, "--json")
        assert retry.returncode != 0
        assert sorted(
            path.name for path in (sandbox.root / "var/runtime/requests").iterdir()
        ) == requests_before
        assert response_path.read_bytes() == response_bytes

        stop_payload = _successful_json(
            sandbox.run("runtime", "stop", "--json"),
            "runtime stop",
        )
        assert _contains(stop_payload, "accepted")
        stopped = True
    finally:
        if started and not stopped:
            sandbox.run("runtime", "stop", "--json")
