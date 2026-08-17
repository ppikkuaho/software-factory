"""Run-scoped daemon lifecycle: one-shot start + on-demand crash protection.

This module owns only process-lifecycle artifacts and launchctl calls. It deliberately does not
import the binding ledger, executor, or spawn chokepoint: ``harnessctl start`` may create the
daemon that will become the state writer, but it can never become a second state-control path.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable, Optional

from harnessd import store


REPO_ROOT = Path(__file__).resolve().parents[1]
PLIST_PROTOTYPE = REPO_ROOT / "harnessd" / "launchd" / "com.harness.daemon.plist"
LIFECYCLE_DIR = Path(".harnessd") / "lifecycle"
IPC_SOCKET = Path(".harnessd") / "harnessd.sock"
INSTANCE_LOCK = Path(".harnessd.instance.lock")
REQUEST_SCHEMA_VERSION = 2
DEFAULT_THROTTLE_SECONDS = 10
READINESS_STDERR_TAIL_LINES = 20
READINESS_STDERR_TAIL_BYTES = 16 * 1024
DAEMON_SYSTEM_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

_TOKENS = {
    "label": "__HARNESSD_LABEL__",
    "python": "__HARNESSD_PYTHON__",
    "repo": "__HARNESSD_REPO_ROOT__",
    "runtime": "__HARNESSD_RUNTIME_ROOT__",
    "build_id": "__HARNESSD_BUILD_ID__",
    "request": "__HARNESSD_START_REQUEST__",
    "request_id": "__HARNESSD_REQUEST_ID__",
    "plist": "__HARNESSD_RUN_PLIST__",
    "stdout": "__HARNESSD_STDOUT__",
    "stderr": "__HARNESSD_STDERR__",
    "path": "__HARNESSD_PATH__",
    "tmux": "__HARNESSD_TMUX__",
    "git": "__HARNESSD_GIT__",
    "sandbox_exec": "__HARNESSD_SANDBOX_EXEC__",
    "security": "__HARNESSD_SECURITY__",
}


class LifecycleError(RuntimeError):
    """A process-lifecycle operation failed before the daemon could own the build."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "lifecycle_error",
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        self.error_code = str(error_code)
        self.details = dict(details or {})


class DaemonAlreadyActive(LifecycleError):
    """Start refusal: this runtime already has a daemon/protective launchd job."""


class LifecycleReadinessError(LifecycleError):
    """Typed start-readiness refusal with bounded launchd/stderr evidence."""


@dataclass(frozen=True)
class LifecyclePaths:
    runtime_root: Path
    directory: Path
    request: Path
    claimed_request: Path
    plist: Path
    stdout_log: Path
    stderr_log: Path
    socket: Path
    instance_lock: Path
    start_guard: Path


@dataclass(frozen=True)
class RunSpec:
    runtime_root: Path
    build_id: str
    label: str
    request_id: str
    paths: LifecyclePaths
    repo_root: Path
    python_executable: str
    daemon_tools: dict[str, str] = field(default_factory=dict)
    daemon_environment: dict[str, str] = field(default_factory=dict)
    _start_guard_handle: Optional[IO] = field(default=None, compare=False, repr=False)


LaunchctlRunner = Callable[[list[str]], object]


def paths_for(runtime_root) -> LifecyclePaths:
    """Return canonical per-run lifecycle paths without creating any artifact."""
    root = Path(runtime_root).expanduser().resolve()
    directory = root / LIFECYCLE_DIR
    return LifecyclePaths(
        runtime_root=root,
        directory=directory,
        request=directory / "start-request.json",
        claimed_request=directory / "start-request.claimed.json",
        plist=directory / "com.harness.daemon.plist",
        stdout_log=directory / "daemon.stdout.log",
        stderr_log=directory / "daemon.stderr.log",
        socket=root / IPC_SOCKET,
        instance_lock=root / INSTANCE_LOCK,
        start_guard=directory / "start-preparation.lock",
    )


def launchd_label(runtime_root, build_id: str) -> str:
    """Derive a legible, collision-safe launchd label for one canonical runtime root."""
    root = Path(runtime_root).expanduser().resolve()
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", str(build_id or "build")).strip("-").lower()
    slug = (slug or "build")[:32]
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return f"com.harness.daemon.{slug}.{digest}"


def _launchctl(args: list[str]):
    return subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def gui_domain(uid: Optional[int] = None) -> str:
    return f"gui/{os.getuid() if uid is None else int(uid)}"


def service_target(label: str, uid: Optional[int] = None) -> str:
    return f"{gui_domain(uid)}/{label}"


def service_is_loaded(
    label: str,
    *,
    launchctl_runner: LaunchctlRunner = _launchctl,
    uid: Optional[int] = None,
) -> bool:
    result = launchctl_runner(["print", service_target(label, uid)])
    return int(getattr(result, "returncode", 1)) == 0


def ipc_socket_is_live(path: Path) -> bool:
    """True only when a process accepts a connection at the canonical daemon socket."""
    path = Path(path)
    if not path.exists():
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.2)
    request_sent = False
    try:
        client.connect(str(path))
        client.sendall(json.dumps({"command": "tree"}).encode("utf-8"))
        request_sent = True
        client.shutdown(socket.SHUT_WR)
        # Consume the small response when available so the probe never creates a broken-pipe journal
        # row. A timeout after a successful connect/send is still live; the instance-lock probe is
        # the final duplicate fence.
        try:
            while client.recv(65536):
                pass
        except (TimeoutError, OSError):
            pass
        return True
    except OSError:
        # Once the daemon accepted a complete request, a reset while reading still proves that a
        # process owns the socket. The instance-lock probe remains the independent final fence.
        return request_sent
    finally:
        client.close()


def instance_lock_is_held(path: Path) -> bool:
    """Probe an existing lifetime-lock file without creating one.

    An unreadable/otherwise unprobeable existing lock is conservatively active.
    """
    path = Path(path)
    if not path.exists():
        return False
    try:
        handle = path.open("r+", encoding="utf-8")
    except OSError:
        return True
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _replace_tokens(value, replacements: dict[str, str]):
    if isinstance(value, str):
        rendered = value
        for token, replacement in replacements.items():
            rendered = rendered.replace(token, replacement)
        return rendered
    if isinstance(value, list):
        return [_replace_tokens(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_tokens(item, replacements) for key, item in value.items()}
    return value


def _resolved_executable(path: str | Path) -> Optional[str]:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return str(resolved)


def _daemon_direct_tools(python_executable: str) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve every binary directly spawned by daemon-reachable Python.

    Pane payloads are intentionally excluded: tmux launches those from their separately isolated
    pane vectors. The daemon itself directly drives tmux, git, sandbox-exec's profile probe, and
    Python (its exact interpreter for cleanup/probe helpers).
    """
    source_dirs = [
        item
        for item in os.environ.get("PATH", "").split(os.pathsep)
        if item
    ]
    search_dirs = list(dict.fromkeys([*source_dirs, *DAEMON_SYSTEM_PATH_DIRS]))
    search_path = os.pathsep.join(search_dirs)
    resolved: dict[str, str] = {}
    missing: dict[str, str] = {}

    python = _resolved_executable(python_executable)
    if python:
        resolved["python"] = python
    else:
        missing["python"] = str(python_executable)

    for name in ("tmux", "git", "sandbox-exec", "security"):
        found = shutil.which(name, path=search_path)
        exact = _resolved_executable(found) if found else None
        if exact:
            resolved[name] = exact
        else:
            missing[name] = f"not found on preparation PATH {search_path!r}"

    return resolved, missing


def _daemon_environment(tools: dict[str, str]) -> dict[str, str]:
    tool_dirs = [str(Path(path).parent) for path in tools.values()]
    frozen_dirs = list(dict.fromkeys([*tool_dirs, *DAEMON_SYSTEM_PATH_DIRS]))
    return {
        "PATH": os.pathsep.join(frozen_dirs),
        "HARNESSD_TMUX": tools["tmux"],
        "HARNESSD_GIT": tools["git"],
        "HARNESSD_SANDBOX_EXEC": tools["sandbox-exec"],
        "HARNESSD_SECURITY": tools["security"],
        "HARNESSD_PYTHON": tools["python"],
    }


def render_launchd_plist(
    spec: RunSpec,
    *,
    prototype: Path = PLIST_PROTOTYPE,
) -> Path:
    """Render the tracked launchd policy prototype into this run's lifecycle directory."""
    with Path(prototype).open("rb") as handle:
        payload = plistlib.load(handle)
    replacements = {
        _TOKENS["label"]: spec.label,
        _TOKENS["python"]: spec.python_executable,
        _TOKENS["repo"]: str(spec.repo_root),
        _TOKENS["runtime"]: str(spec.runtime_root),
        _TOKENS["build_id"]: spec.build_id,
        _TOKENS["request"]: str(spec.paths.request),
        _TOKENS["request_id"]: spec.request_id,
        _TOKENS["plist"]: str(spec.paths.plist),
        _TOKENS["stdout"]: str(spec.paths.stdout_log),
        _TOKENS["stderr"]: str(spec.paths.stderr_log),
        _TOKENS["path"]: spec.daemon_environment["PATH"],
        _TOKENS["tmux"]: spec.daemon_environment["HARNESSD_TMUX"],
        _TOKENS["git"]: spec.daemon_environment["HARNESSD_GIT"],
        _TOKENS["sandbox_exec"]: spec.daemon_environment["HARNESSD_SANDBOX_EXEC"],
        _TOKENS["security"]: spec.daemon_environment["HARNESSD_SECURITY"],
    }
    rendered = _replace_tokens(payload, replacements)

    # plistlib writes bytes; use its own atomic sibling so readers/launchctl never see a partial XML.
    spec.paths.directory.mkdir(parents=True, exist_ok=True)
    temp = spec.paths.plist.with_name(f".{spec.paths.plist.name}.tmp")
    try:
        with temp.open("wb") as handle:
            plistlib.dump(rendered, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, spec.paths.plist)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
    return spec.paths.plist


def _write_start_request(
    spec: RunSpec,
    *,
    initial_intake: Optional[str],
    fidelity_playback_authority: str,
    fidelity_playback_delegate: Optional[str],
    fidelity_playback_delegation_reason: Optional[str],
    review_panel_arms: Optional[list[dict]],
) -> Path:
    payload = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": spec.request_id,
        "build_id": spec.build_id,
        "runtime_root": str(spec.runtime_root),
        "initial_intake": initial_intake,
        "fidelity_playback_authority": fidelity_playback_authority,
        "fidelity_playback_delegate": fidelity_playback_delegate,
        "fidelity_playback_delegation_reason": (
            fidelity_playback_delegation_reason
        ),
    }
    if review_panel_arms:
        payload["review_panel_arms"] = review_panel_arms

    def render(handle):
        os.fchmod(handle.fileno(), 0o600)
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")

    store.atomic_replace(spec.paths.request, render)
    return spec.paths.request


def prepare_run(
    *,
    runtime_root,
    build_id: str,
    initial_intake: Optional[str] = None,
    fidelity_playback_authority: str = "owner",
    fidelity_playback_delegate: Optional[str] = None,
    fidelity_playback_delegation_reason: Optional[str] = None,
    review_panel_arms: Optional[list[dict]] = None,
    repo_root: Optional[Path] = None,
    python_executable: Optional[str] = None,
    ipc_probe: Callable[[Path], bool] = ipc_socket_is_live,
    lock_probe: Callable[[Path], bool] = instance_lock_is_held,
    launchctl_runner: LaunchctlRunner = _launchctl,
    uid: Optional[int] = None,
) -> RunSpec:
    """Fence duplicate start, then create the one-shot request and rendered per-run plist."""
    paths = paths_for(runtime_root)
    label = launchd_label(paths.runtime_root, build_id)

    # ORDER IS BINDING: all three probes precede directory/request/plist creation.
    if ipc_probe(paths.socket):
        raise DaemonAlreadyActive(
            f"daemon already reachable at {paths.socket}; refusing duplicate start"
        )
    if lock_probe(paths.instance_lock):
        raise DaemonAlreadyActive(
            f"daemon instance lock is held at {paths.instance_lock}; refusing duplicate start"
        )
    if service_is_loaded(label, launchctl_runner=launchctl_runner, uid=uid):
        raise DaemonAlreadyActive(
            f"protective launchd job {service_target(label, uid)} is already loaded; "
            "refusing duplicate start"
        )

    resolved_repo_root = Path(repo_root or REPO_ROOT).expanduser().resolve()
    requested_python = str(python_executable or sys.executable)
    daemon_tools, missing_tools = _daemon_direct_tools(requested_python)
    if missing_tools:
        listing = "; ".join(f"{name}: {reason}" for name, reason in sorted(missing_tools.items()))
        raise LifecycleError(
            f"daemon direct-tool preflight failed; missing tools: {listing}",
            error_code="daemon_tools_missing",
            details={"missing_tools": missing_tools},
        )

    # Serialize the probe->publish->bootstrap window among concurrent harnessctl start callers.
    # This guard is acquired only AFTER the required triple no-write probe above. Its flock dies if
    # the CLI crashes; the file may persist unlocked and is not control state.
    try:
        guard = store.flock_exclusive_nb(paths.start_guard)
    except BlockingIOError as exc:
        raise DaemonAlreadyActive(
            f"another start preparation owns {paths.start_guard}; refusing duplicate start"
        ) from exc

    # Re-probe under the guard to close the interval between the first probes and guard acquisition.
    # If a previous CLI crashed while holding the guard, no process can still own its unpublished
    # lifecycle files; after these probes prove no daemon/job, remove those stale remnants.
    try:
        if ipc_probe(paths.socket):
            raise DaemonAlreadyActive(
                f"daemon already reachable at {paths.socket}; refusing duplicate start"
            )
        if lock_probe(paths.instance_lock):
            raise DaemonAlreadyActive(
                f"daemon instance lock is held at {paths.instance_lock}; refusing duplicate start"
            )
        if service_is_loaded(label, launchctl_runner=launchctl_runner, uid=uid):
            raise DaemonAlreadyActive(
                f"protective launchd job {service_target(label, uid)} is already loaded; "
                "refusing duplicate start"
            )
        _remove_generated_artifacts(paths)
    except BaseException:
        guard.close()
        raise

    spec = RunSpec(
        runtime_root=paths.runtime_root,
        build_id=str(build_id),
        label=label,
        request_id=uuid.uuid4().hex,
        paths=paths,
        repo_root=resolved_repo_root,
        python_executable=daemon_tools["python"],
        daemon_tools=daemon_tools,
        daemon_environment=_daemon_environment(daemon_tools),
        _start_guard_handle=guard,
    )
    try:
        _write_start_request(
            spec,
            initial_intake=initial_intake,
            fidelity_playback_authority=fidelity_playback_authority,
            fidelity_playback_delegate=fidelity_playback_delegate,
            fidelity_playback_delegation_reason=(
                fidelity_playback_delegation_reason
            ),
            review_panel_arms=review_panel_arms,
        )
        render_launchd_plist(spec)
    except BaseException:
        _remove_generated_artifacts(paths)
        release_start_guard(spec)
        raise
    return spec


def release_start_guard(spec) -> None:
    """Release a harnessctl start preparation guard; safe for reconstructed daemon specs."""
    handle = getattr(spec, "_start_guard_handle", None)
    if handle is None or getattr(handle, "closed", False):
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_request(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot read lifecycle start request {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise LifecycleError(
            f"invalid lifecycle start request {path}: expected schema_version "
            f"{REQUEST_SCHEMA_VERSION}"
        )
    for key in ("request_id", "build_id", "runtime_root"):
        if not payload.get(key):
            raise LifecycleError(f"invalid lifecycle start request {path}: missing {key}")
    for key in (
        "fidelity_playback_authority",
        "fidelity_playback_delegate",
        "fidelity_playback_delegation_reason",
    ):
        if key not in payload:
            raise LifecycleError(
                f"invalid lifecycle start request {path}: missing {key}"
            )
    return payload


def peek_start_request(path: Path) -> Optional[dict]:
    """Read the pending request for runtime assembly without consuming it."""
    path = Path(path)
    if not path.exists():
        return None
    return _read_request(path)


def claim_start_request(
    request_path: Path,
    claimed_path: Path,
    *,
    expected_request_id: Optional[str] = None,
) -> Optional[dict]:
    """Atomically consume one genesis authority; absent means crash-recovery mode."""
    request_path = Path(request_path)
    claimed_path = Path(claimed_path)
    if not request_path.exists():
        return None
    payload = _read_request(request_path)
    if expected_request_id is not None and payload.get("request_id") != expected_request_id:
        raise LifecycleError(
            f"lifecycle request id mismatch: expected {expected_request_id!r}, "
            f"found {payload.get('request_id')!r}"
        )
    claimed_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(request_path, claimed_path)
    return payload


def _result_error(result) -> str:
    return str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()


def _launchctl_scalar(text: str, key: str) -> Optional[str]:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", text or "")
    return match.group(1).strip() if match else None


def _bounded_stderr_tail(path: Path) -> list[str]:
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - READINESS_STDERR_TAIL_BYTES))
            raw = handle.read(READINESS_STDERR_TAIL_BYTES)
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[-READINESS_STDERR_TAIL_LINES:]


def readiness_error(
    spec: RunSpec,
    *,
    timeout_s: float,
    socket_present: bool,
    last_l1_error: str,
    launchctl_runner: LaunchctlRunner = _launchctl,
    uid: Optional[int] = None,
) -> LifecycleReadinessError:
    """Classify one bounded start timeout from the existing launchd job and stderr evidence."""
    target = service_target(spec.label, uid)
    result = launchctl_runner(["print", target])
    loaded = int(getattr(result, "returncode", 1)) == 0
    output = str(getattr(result, "stdout", "") or "")
    service_state = _launchctl_scalar(output, "state") if loaded else "not_loaded"
    raw_pid = _launchctl_scalar(output, "pid")
    raw_runs = _launchctl_scalar(output, "runs")
    raw_exit = _launchctl_scalar(output, "last exit code")
    try:
        pid = int(raw_pid) if raw_pid is not None else None
    except ValueError:
        pid = None
    try:
        restart_count = int(raw_runs) if raw_runs is not None else 0
    except ValueError:
        restart_count = 0
    try:
        last_exit_code = int(raw_exit) if raw_exit is not None else None
    except ValueError:
        last_exit_code = None
    process_alive = service_state == "running" and pid is not None and pid > 0
    stderr_tail = _bounded_stderr_tail(spec.paths.stderr_log)
    details = {
        "service_target": target,
        "service_loaded": loaded,
        "service_state": service_state,
        "pid": pid,
        "process_alive": process_alive,
        "restart_count": restart_count,
        "last_exit_code": last_exit_code,
        "socket_present": bool(socket_present),
        "stderr_tail": stderr_tail,
        "interpreter_path": spec.python_executable,
    }

    if socket_present:
        return LifecycleReadinessError(
            f"start readiness timed out after {float(timeout_s):g}s: daemon IPC answered but "
            f"L1 was not ready ({last_l1_error or 'no live L1 binding'})",
            error_code="l1_not_ready",
            details=details,
        )
    crash_evidence = (
        not process_alive
        or restart_count > 1
        or (last_exit_code is not None and last_exit_code != 0)
    )
    if not crash_evidence:
        if not stderr_tail:
            message = (
                f"start readiness timed out after {float(timeout_s):g}s: daemon process {pid} is "
                f"alive but no socket appeared and stderr is empty. Likely cause: macOS TCC is "
                f"blocking interpreter {spec.python_executable} from the Documents-folder "
                f"repository {spec.repo_root}. A Homebrew Python upgrade moves the Cellar binary "
                "path and can orphan the prior grant. Fix: in System Settings > Privacy & Security "
                f"> Files & Folders, grant Documents Folder access to {spec.python_executable}, "
                "then rerun harnessctl start."
            )
        else:
            message = (
                f"start readiness timed out after {float(timeout_s):g}s: daemon process {pid} is "
                "alive but no socket appeared; inspect the attached stderr tail"
            )
        return LifecycleReadinessError(
            message,
            error_code="daemon_hung",
            details=details,
        )
    return LifecycleReadinessError(
        f"start readiness timed out after {float(timeout_s):g}s: daemon is not running "
        f"(launchd state={service_state!r}, restart_count={restart_count}); "
        "the bounded stderr tail is attached",
        error_code="daemon_crashed",
        details=details,
    )


def _remove_generated_artifacts(paths: LifecyclePaths) -> None:
    for path in (paths.request, paths.claimed_request, paths.plist):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def bootstrap_run(
    spec: RunSpec,
    *,
    launchctl_runner: LaunchctlRunner = _launchctl,
    uid: Optional[int] = None,
) -> None:
    """Bootstrap and explicitly kickstart the per-run service; rollback lifecycle-only on failure."""
    domain = gui_domain(uid)
    bootstrapped = False
    try:
        result = launchctl_runner(["bootstrap", domain, str(spec.paths.plist)])
        if int(getattr(result, "returncode", 1)) != 0:
            raise LifecycleError(
                f"launchctl bootstrap failed for {spec.label}: {_result_error(result)}"
            )
        bootstrapped = True
        result = launchctl_runner(["kickstart", service_target(spec.label, uid)])
        if int(getattr(result, "returncode", 1)) != 0:
            raise LifecycleError(
                f"launchctl kickstart failed for {spec.label}: {_result_error(result)}"
            )
    except BaseException:
        if bootstrapped:
            cleanup_run(
                label=spec.label,
                paths=spec.paths,
                launchctl_runner=launchctl_runner,
                uid=uid,
            )
        else:
            _remove_generated_artifacts(spec.paths)
        raise


def cleanup_run(
    *,
    label: str,
    paths: LifecyclePaths,
    launchctl_runner: LaunchctlRunner = _launchctl,
    uid: Optional[int] = None,
    attempts: int = 3,
    retry_delay_s: float = 0.1,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Boot out one dormant per-run job, retaining its repair pointer on bounded failure."""
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        result = launchctl_runner(["bootout", service_target(label, uid)])
        if int(getattr(result, "returncode", 1)) == 0:
            _remove_generated_artifacts(paths)
            return True

        # launchctl reports an error when the service is already absent. Verify that condition
        # before treating the cleanup as failed; an unprobeable service is conservatively loaded.
        try:
            loaded = service_is_loaded(label, launchctl_runner=launchctl_runner, uid=uid)
        except Exception:
            loaded = True
        if not loaded:
            _remove_generated_artifacts(paths)
            return True
        if attempt + 1 < attempts:
            sleep_fn(retry_delay_s)

    # Keep the plist and claimed request: they are the exact evidence/repair pointer an operator
    # needs when a still-loaded launchd job could not be removed.
    return False


def schedule_cleanup_after_exit(
    spec,
    *,
    parent_pid: int,
    uid: Optional[int] = None,
) -> int:
    """Start the bounded post-idle janitor; this seam is never called on crash paths."""
    argv = [
        sys.executable,
        "-m",
        "harnessd.lifecycle",
        "cleanup-after-pid",
        "--parent-pid",
        str(int(parent_pid)),
        "--runtime-root",
        str(spec.paths.runtime_root),
        "--label",
        str(spec.label),
        "--uid",
        str(os.getuid() if uid is None else int(uid)),
    ]
    spec.paths.directory.mkdir(parents=True, exist_ok=True)
    with spec.paths.stderr_log.open("ab") as stderr_log:
        child = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            close_fds=True,
            start_new_session=True,
        )
    return int(child.pid)


def _wait_for_parent_exit(parent_pid: int, *, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harnessd.lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)
    cleanup = sub.add_parser("cleanup-after-pid")
    cleanup.add_argument("--parent-pid", type=int, required=True)
    cleanup.add_argument("--runtime-root", required=True)
    cleanup.add_argument("--label", required=True)
    cleanup.add_argument("--uid", type=int, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "cleanup-after-pid":
        if not _wait_for_parent_exit(args.parent_pid):
            return 2
        cleaned = cleanup_run(
            label=args.label,
            paths=paths_for(args.runtime_root),
            uid=args.uid,
        )
        if not cleaned:
            print(
                f"idle cleanup could not boot out {service_target(args.label, args.uid)}; "
                "retained lifecycle artifacts for recovery",
                file=sys.stderr,
            )
            return 3
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised as the detached cleanup entry
    raise SystemExit(main())
