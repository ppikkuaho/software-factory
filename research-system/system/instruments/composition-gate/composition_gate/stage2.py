"""Packet-only stage-2 generation and validation.

This module prepares evidence for a later, atomic cgate finalizer.  It has no
Tier-1 mutation or CLI surface: callers supply an already prepared packet and
receive a fully attributed preparation result.  Real generation is always
detached from the research root and protected by an injected OS sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Mapping, Protocol, Sequence

from .packet import (
    MANIFEST_FORMAT,
    PROMPT_NAME,
    PROMPT_SHA256,
    PacketError,
    PreparedPacket,
    review_prompt_bytes,
)


RAW_OUTPUT_FORMAT = "composition-gate-raw-output.v1"
DECISION_FORMAT = "composition-gate-stage2-decision.v1"
REQUESTED_MODEL = "opus"
ALLOWED_TOOLS = ("Read", "Glob", "Grep")
DEFAULT_TIMEOUT_SECONDS = 600
MAX_STDOUT_BYTES = 32 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_MODEL_BODY_BYTES = 128 * 1024
MAX_NOTE_CHARACTERS = 16 * 1024
MAX_OBSERVATION_CHARACTERS = 16 * 1024
MAX_OBSERVATIONS = 256

VERDICTS = frozenset(
    {
        "land",
        "land-after-X",
        "consolidate-first",
        "bounce-for-surface-rework",
        "hold",
        "escalate-to-user",
        "escalate-stuck",
    }
)
_LAND_AFTER = re.compile(r"land-after-(.+)")
_MANIFEST_REF = re.compile(
    r"var/cgate/(MR-(?:0|[1-9][0-9]*))/attempts/([0-9a-f]{32})/packet/manifest\.json"
)
_RAW_REF = re.compile(
    r"var/cgate/(MR-(?:0|[1-9][0-9]*))/attempts/([0-9a-f]{32})/raw-output\.json"
)
_UNBLOCK_WORDS = (
    "until",
    "after",
    "unblock",
    "resolve",
    "resolved",
    "clear",
    "clears",
    "settle",
    "settled",
    "sequence",
    "rework",
    "consolidat",
)
_STUCK_WORDS = (
    "missing",
    "contradict",
    "unusable",
    "broken",
    "malformed",
    "invalid",
    "mismatch",
    "unavailable",
    "failure",
    "failed",
    "cannot",
    "denied",
)
_STUCK_SUBJECTS = (
    "evidence",
    "artifact",
    "reference",
    "hash",
    "manifest",
    "packet",
    "screen",
    "output",
    "session",
    "model",
    "sandbox",
    "path",
    "field",
    "identity",
    "data",
)


class Stage2Error(RuntimeError):
    """Stable, sanitized failure from stage-2 preparation."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


class SandboxError(Stage2Error):
    """The required OS sandbox could not be established or proved."""


@dataclass(frozen=True)
class DetachedPacket:
    """Verified temporary packet and isolated HOME outside the research root."""

    root: Path
    packet_dir: Path
    home_dir: Path
    manifest: dict[str, Any]
    artifact_hashes: dict[str, str]


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    detached: DetachedPacket
    research_root: Path


@dataclass(frozen=True)
class GenerationResult:
    """Raw generator result; parsing and evidence writing belong to the runner."""

    mechanism: str
    stdout: bytes = b""
    stderr: bytes = b""
    return_code: int | None = 0
    technical_error: str | None = None
    technical_detail: str | None = None
    synthetic_session_id: str | None = None

    @classmethod
    def synthetic(
        cls,
        body: Mapping[str, Any] | str | bytes,
        *,
        session_id: str = "synthetic-session",
    ) -> "GenerationResult":
        if isinstance(body, Mapping):
            raw = _canonical_bytes(dict(body))
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        return cls(
            mechanism="injected-synthetic",
            stdout=raw,
            synthetic_session_id=session_id,
        )


class Generator(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Return raw generation data without interpreting the model body."""


@dataclass(frozen=True)
class SandboxLaunch:
    command_prefix: tuple[str, ...]
    policy: str


class SandboxController(Protocol):
    def prepare(
        self,
        *,
        detached: DetachedPacket,
        research_root: Path,
        executable: Path,
    ) -> SandboxLaunch:
        """Build a filesystem policy or raise ``SandboxError``."""

    def probe(
        self,
        launch: SandboxLaunch,
        *,
        detached: DetachedPacket,
        research_root: Path,
        environment: Mapping[str, str],
    ) -> None:
        """Prove packet access and research-root denial or raise."""


@dataclass(frozen=True)
class RawOutputRef:
    ref: str
    sha256: str


@dataclass(frozen=True)
class ValidatedDecision:
    verdict: str
    note: str
    observations: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Stage2Preparation:
    attempt_id: str
    packet: dict[str, Any]
    template: dict[str, str]
    generator: dict[str, Any]
    verdict: str
    note: str
    observations: tuple[dict[str, Any], ...]
    raw_output: RawOutputRef
    decision_output: RawOutputRef
    error_kind: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "packet": self.packet,
            "template": self.template,
            "generator": self.generator,
            "verdict": self.verdict,
            "note": self.note,
            "observations": list(self.observations),
            "raw_output": {
                "ref": self.raw_output.ref,
                "sha256": self.raw_output.sha256,
            },
            "decision_output": {
                "ref": self.decision_output.ref,
                "sha256": self.decision_output.sha256,
            },
            "error_kind": self.error_kind,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Stage2Error("evidence-serialization-failure", str(exc)) from exc


def _safe_ref(value: Any, *, allow_manifest: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Stage2Error("detached-manifest-invalid", "manifest contains an unsafe artifact ref")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) < 2 or path.parts[0:1] != ("packet",) or any(
        part in {"", ".", ".."} for part in path.parts
    ) or path.as_posix() != value or (value == "packet/manifest.json" and not allow_manifest):
        raise Stage2Error("detached-manifest-invalid", "manifest contains an unsafe artifact ref")
    return value


def _manifest_for(prepared: PreparedPacket) -> tuple[bytes, dict[str, Any], dict[str, str]]:
    manifest_path = prepared.packet_dir / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise Stage2Error("detached-manifest-missing", "prepared manifest is unreadable") from exc
    actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_hash != prepared.manifest_sha256:
        raise Stage2Error("detached-manifest-mismatch", "prepared manifest hash mismatch")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage2Error("detached-manifest-invalid", "prepared manifest is not valid UTF-8 JSON") from exc
    required = {
        "format",
        "attempt_id",
        "record_id",
        "snapshot",
        "artifacts",
        "artifact_refs",
        "input_hashes",
        "source_refs",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise Stage2Error("detached-manifest-invalid", "prepared manifest has the wrong fields")
    if manifest.get("format") != MANIFEST_FORMAT or manifest.get("attempt_id") != prepared.attempt_id:
        raise Stage2Error("detached-manifest-invalid", "prepared manifest identity mismatch")
    manifest_match = _MANIFEST_REF.fullmatch(prepared.manifest_ref)
    if (
        manifest_match is None
        or manifest_match.group(1) != manifest.get("record_id")
        or manifest_match.group(2) != prepared.attempt_id
    ):
        raise Stage2Error("detached-manifest-invalid", "prepared manifest ref and identity disagree")
    snapshot = manifest.get("snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {"head_commit", "head_tree"}:
        raise Stage2Error("detached-manifest-invalid", "prepared manifest snapshot is malformed")
    if any(
        not isinstance(snapshot.get(field), str)
        or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", snapshot[field]) is None
        for field in ("head_commit", "head_tree")
    ):
        raise Stage2Error("detached-manifest-invalid", "prepared manifest snapshot identity is malformed")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise Stage2Error("detached-manifest-invalid", "prepared manifest has no artifacts")
    hashes: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "source_ref",
            "git_oid",
            "packet_ref",
            "artifact_kind",
            "sha256",
        }:
            raise Stage2Error("detached-manifest-invalid", "prepared manifest artifact is malformed")
        ref = _safe_ref(artifact.get("packet_ref"))
        source_ref = artifact.get("source_ref")
        artifact_kind = artifact.get("artifact_kind")
        git_oid = artifact.get("git_oid")
        if (
            not isinstance(source_ref, str)
            or not source_ref
            or "\\" in source_ref
            or source_ref.startswith("/")
            or (
                source_ref.startswith("generated:")
                and re.fullmatch(r"generated:[A-Za-z0-9._-]+", source_ref) is None
            )
            or (
                not source_ref.startswith("generated:")
                and (
                    PurePosixPath(source_ref).as_posix() != source_ref
                    or any(part in {"", ".", ".."} for part in PurePosixPath(source_ref).parts)
                )
            )
            or not isinstance(artifact_kind, str)
            or not artifact_kind
            or (
                git_oid is not None
                and (
                    not isinstance(git_oid, str)
                    or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", git_oid) is None
                )
            )
        ):
            raise Stage2Error("detached-manifest-invalid", "prepared manifest artifact identity is malformed")
        digest = artifact.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise Stage2Error("detached-manifest-invalid", "prepared manifest artifact hash is malformed")
        if ref in hashes:
            raise Stage2Error("detached-manifest-invalid", "prepared manifest repeats an artifact ref")
        hashes[ref] = digest
    if manifest.get("artifact_refs") != list(hashes):
        raise Stage2Error("detached-manifest-invalid", "manifest artifact_refs disagree with artifacts")
    if manifest.get("input_hashes") != hashes:
        raise Stage2Error("detached-manifest-invalid", "manifest input_hashes disagree with artifacts")
    source_refs = manifest.get("source_refs")
    if not isinstance(source_refs, dict) or set(source_refs) != set(hashes):
        raise Stage2Error("detached-manifest-invalid", "manifest source_refs disagree with artifacts")
    if source_refs != {row["packet_ref"]: row["source_ref"] for row in artifacts}:
        raise Stage2Error("detached-manifest-invalid", "manifest source_refs disagree with artifact rows")
    if tuple(hashes) != prepared.artifact_refs or hashes != prepared.input_hashes:
        raise Stage2Error("detached-manifest-mismatch", "prepared packet metadata disagrees with manifest")
    return manifest_bytes, manifest, hashes


def _packet_files(packet_dir: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    try:
        for directory, dirnames, filenames in os.walk(packet_dir, followlinks=False):
            parent = Path(directory)
            for name in (*dirnames, *filenames):
                if (parent / name).is_symlink():
                    raise Stage2Error("detached-path-unsafe", "packet contains a symbolic link")
            for name in filenames:
                path = parent / name
                if not path.is_file():
                    raise Stage2Error("detached-path-unsafe", "packet contains a non-regular file")
                relative = path.relative_to(packet_dir.parent).as_posix()
                _safe_ref(relative, allow_manifest=True)
                files[relative] = path
    except OSError as exc:
        raise Stage2Error("detached-copy-failure", "cannot enumerate prepared packet") from exc
    return files


def _packet_directories(packet_dir: Path) -> set[str]:
    directories: set[str] = set()
    for directory, dirnames, _filenames in os.walk(packet_dir, followlinks=False):
        parent = Path(directory)
        for name in dirnames:
            path = parent / name
            if path.is_symlink() or not path.is_dir():
                raise Stage2Error("detached-path-unsafe", "packet contains an unsafe directory")
            directories.add(path.relative_to(packet_dir.parent).as_posix())
    return directories


def _expected_directories(refs: Sequence[str]) -> set[str]:
    expected: set[str] = set()
    for ref in refs:
        parent = PurePosixPath(ref).parent
        while parent.as_posix() != "packet":
            expected.add(parent.as_posix())
            parent = parent.parent
    return expected


def _mkdir_exclusive(path: Path, mode: int = 0o700) -> None:
    try:
        os.mkdir(path, mode)
    except OSError as exc:
        raise Stage2Error("detached-copy-failure", "cannot create detached packet directory") from exc


def _write_exclusive(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise Stage2Error("evidence-collision", "refusing to overwrite stage-2 evidence") from exc
    except OSError as exc:
        raise Stage2Error("evidence-write-failure", "cannot write stage-2 evidence") from exc


def detach_packet(
    prepared: PreparedPacket,
    research_root: str | Path,
    *,
    temp_factory: Callable[..., str] = tempfile.mkdtemp,
) -> DetachedPacket:
    """Copy and rehash a completed packet into fresh private temporary roots."""

    root = Path(research_root).expanduser().resolve()
    attempt_dir = prepared.attempt_dir.resolve()
    if not attempt_dir.is_relative_to(root) or prepared.packet_dir.resolve() != attempt_dir / "packet":
        raise Stage2Error("detached-path-unsafe", "prepared packet is outside its research root attempt")
    if (root / prepared.manifest_ref).resolve() != prepared.packet_dir.resolve() / "manifest.json":
        raise Stage2Error("detached-path-unsafe", "prepared manifest ref does not resolve to the packet")
    manifest_bytes, manifest, hashes = _manifest_for(prepared)
    source_files = _packet_files(prepared.packet_dir)
    expected = set(hashes) | {"packet/manifest.json"}
    if set(source_files) != expected:
        raise Stage2Error("detached-extra-path", "prepared packet has missing or extra files")
    if _packet_directories(prepared.packet_dir) != _expected_directories(tuple(expected)):
        raise Stage2Error("detached-extra-path", "prepared packet has extra directories")
    for ref, expected_hash in hashes.items():
        try:
            actual = hashlib.sha256(source_files[ref].read_bytes()).hexdigest()
        except OSError as exc:
            raise Stage2Error("detached-artifact-missing", "prepared artifact is unreadable") from exc
        if actual != expected_hash:
            raise Stage2Error("detached-artifact-mismatch", f"prepared artifact hash mismatch for {ref}")

    detached_root: Path | None = None
    home_dir: Path | None = None
    try:
        raw_detached_root = Path(temp_factory(prefix="ht-cgate-detached-"))
        if raw_detached_root.is_symlink():
            raise Stage2Error("detached-root-unsafe", "temporary roots must not be symbolic links")
        detached_root = raw_detached_root.resolve()
        raw_home_dir = Path(temp_factory(prefix="ht-cgate-home-"))
        if raw_home_dir.is_symlink():
            raise Stage2Error("detached-root-unsafe", "temporary roots must not be symbolic links")
        home_dir = raw_home_dir.resolve()
        if (
            detached_root.is_relative_to(root)
            or home_dir.is_relative_to(root)
            or home_dir == detached_root
            or home_dir.is_relative_to(detached_root)
            or detached_root.is_relative_to(home_dir)
            or not detached_root.is_dir()
            or not home_dir.is_dir()
            or any(detached_root.iterdir())
            or any(home_dir.iterdir())
        ):
            raise Stage2Error("detached-root-unsafe", "temporary roots overlap protected paths")
        os.chmod(detached_root, 0o700)
        os.chmod(home_dir, 0o700)
        detached_packet_dir = detached_root / "packet"
        _mkdir_exclusive(detached_packet_dir)
        for ref in hashes:
            destination = detached_root / PurePosixPath(ref)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_exclusive(destination, source_files[ref].read_bytes())
        _write_exclusive(detached_packet_dir / "manifest.json", manifest_bytes)

        detached_files = _packet_files(detached_packet_dir)
        if set(detached_files) != expected:
            raise Stage2Error("detached-extra-path", "detached packet has missing or extra files")
        if _packet_directories(detached_packet_dir) != _expected_directories(tuple(expected)):
            raise Stage2Error("detached-extra-path", "detached packet has extra directories")
        if hashlib.sha256(detached_files["packet/manifest.json"].read_bytes()).hexdigest() != prepared.manifest_sha256:
            raise Stage2Error("detached-manifest-mismatch", "detached manifest bytes changed")
        for ref, expected_hash in hashes.items():
            if hashlib.sha256(detached_files[ref].read_bytes()).hexdigest() != expected_hash:
                raise Stage2Error("detached-artifact-mismatch", f"detached artifact hash mismatch for {ref}")
        return DetachedPacket(detached_root, detached_packet_dir, home_dir, manifest, hashes)
    except Exception:
        if detached_root is not None:
            shutil.rmtree(detached_root, ignore_errors=True)
        if home_dir is not None:
            shutil.rmtree(home_dir, ignore_errors=True)
        raise


def cleanup_detached(detached: DetachedPacket) -> None:
    shutil.rmtree(detached.root, ignore_errors=True)
    shutil.rmtree(detached.home_dir, ignore_errors=True)


def _sandbox_literal(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacOSSandbox:
    """``sandbox-exec`` policy and direct filesystem-semantics probe."""

    def __init__(
        self,
        *,
        sandbox_binary: str | Path | None = None,
        runtime_read_paths: Sequence[str | Path] = (),
        run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self._sandbox_binary = Path(sandbox_binary).resolve() if sandbox_binary else None
        self._runtime_read_paths = tuple(Path(path).expanduser().resolve() for path in runtime_read_paths)
        self._run = run

    def _binary(self) -> Path:
        if platform.system() != "Darwin":
            raise SandboxError("sandbox-unsupported", "the required filesystem sandbox is unavailable on this platform")
        candidate = self._sandbox_binary or Path(shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec")
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise SandboxError("sandbox-unavailable", "sandbox-exec is not executable")
        return candidate.resolve()

    def prepare(
        self,
        *,
        detached: DetachedPacket,
        research_root: Path,
        executable: Path,
    ) -> SandboxLaunch:
        sandbox_binary = self._binary()
        home = Path.home().resolve()
        fixed_reads = (
            Path("/System"),
            Path("/Library"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/private/etc"),
            Path("/opt/homebrew"),
            Path("/usr/local"),
            home / ".claude",
            home / ".claude.json",
            home / ".config" / "claude",
            home / ".local" / "bin",
            home / ".local" / "share" / "claude",
            home / ".cache" / "claude",
            home / "Library" / "Application Support" / "Claude",
        )
        read_paths = tuple(dict.fromkeys((*fixed_reads, executable.resolve(), *self._runtime_read_paths)))
        if any(
            path == research_root
            or path.is_relative_to(research_root)
            or research_root.is_relative_to(path)
            for path in read_paths
        ):
            raise SandboxError("sandbox-configuration-invalid", "runtime allowlist overlaps the research root")
        read_rules = "\n".join(f"(allow file-read* (subpath {_sandbox_literal(path)}))" for path in read_paths)
        policy = "\n".join(
            (
                "(version 1)",
                "(deny default)",
                '(import "system.sb")',
                "(allow process*)",
                "(allow network*)",
                "(allow sysctl-read)",
                "(allow mach-lookup)",
                read_rules,
                f"(allow file-read* (subpath {_sandbox_literal(detached.root)}))",
                f"(allow file-read* file-write* (subpath {_sandbox_literal(detached.home_dir)}))",
                f"(deny file-read* file-write* (subpath {_sandbox_literal(research_root)}))",
            )
        )
        return SandboxLaunch((str(sandbox_binary), "-p", policy), policy)

    def probe(
        self,
        launch: SandboxLaunch,
        *,
        detached: DetachedPacket,
        research_root: Path,
        environment: Mapping[str, str],
    ) -> None:
        outside = detached.root.parent / f"{detached.root.name}-parent-access-must-fail"
        outside.write_bytes(b"deny\n")
        denied_write = research_root / ".ht-cgate-sandbox-probe"
        if denied_write.exists():
            raise SandboxError("sandbox-probe-unsafe", "reserved sandbox probe path already exists")
        read_candidates = (
            research_root / ".git",
            research_root / ".git" / "HEAD",
            research_root / "pyproject.toml",
        )
        read_target = next((path for path in read_candidates if path.is_file()), None)
        if read_target is None:
            outside.unlink(missing_ok=True)
            raise SandboxError("sandbox-probe-unsafe", "no stable research-root probe file exists")
        script = (
            "set -eu\n"
            "test -r packet/manifest.json\n"
            f"if cat ../{outside.name} >/dev/null 2>&1; then exit 41; fi\n"
            f"if cat {_shell_quote(read_target)} >/dev/null 2>&1; then exit 42; fi\n"
            f"if touch {_shell_quote(denied_write)} >/dev/null 2>&1; then exit 43; fi\n"
            "exit 0\n"
        )
        try:
            result = self._run(
                [*launch.command_prefix, "/bin/sh", "-c", script],
                cwd=detached.root,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError("sandbox-probe-failure", "sandbox filesystem probe did not complete") from exc
        finally:
            outside.unlink(missing_ok=True)
        if denied_write.exists():
            denied_write.unlink(missing_ok=True)
            raise SandboxError("sandbox-probe-failure", "sandbox probe permitted a research-root write")
        if result.returncode != 0:
            raise SandboxError("sandbox-probe-failure", f"sandbox filesystem probe failed with status {result.returncode}")


def _shell_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def child_environment(detached: DetachedPacket, research_root: Path) -> dict[str, str]:
    """Build a small child environment with no research-root path value."""

    root_text = str(research_root.resolve())
    selected = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
    )
    environment = {
        key: value
        for key in selected
        if (value := os.environ.get(key)) is not None and root_text not in value
    }
    path_parts = [
        part
        for part in environment.get("PATH", os.defpath).split(os.pathsep)
        if part and root_text not in str(Path(part).expanduser())
    ]
    environment["PATH"] = os.pathsep.join(path_parts) or os.defpath
    environment["HOME"] = str(detached.home_dir)
    environment["TMPDIR"] = str(detached.home_dir)
    environment["PWD"] = str(detached.root)
    if "CLAUDE_CONFIG_DIR" not in environment:
        default_config = Path.home().resolve() / ".claude"
        if root_text not in str(default_config):
            environment["CLAUDE_CONFIG_DIR"] = str(default_config)
    if any(root_text in value for value in environment.values()):
        raise Stage2Error("child-environment-unsafe", "child environment contains the research root")
    return environment


class ClaudeGenerator:
    """Real, sandbox-mandatory headless ``claude -p`` driver."""

    def __init__(
        self,
        *,
        sandbox: SandboxController | None = None,
        binary: str | Path | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self._sandbox = sandbox or MacOSSandbox()
        self._binary = Path(binary).expanduser().resolve() if binary else None
        self._timeout = timeout_seconds
        self._run = run

    def _find_binary(self) -> Path:
        candidate = self._binary
        if candidate is None:
            found = shutil.which("claude")
            if found is None:
                raise Stage2Error("generator-unavailable", "claude executable was not found")
            candidate = Path(found).resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise Stage2Error("generator-unavailable", "claude executable is not executable")
        return candidate

    def generate(self, request: GenerationRequest) -> GenerationResult:
        try:
            executable = self._find_binary()
            environment = child_environment(request.detached, request.research_root)
            launch = self._sandbox.prepare(
                detached=request.detached,
                research_root=request.research_root,
                executable=executable,
            )
            self._sandbox.probe(
                launch,
                detached=request.detached,
                research_root=request.research_root,
                environment=environment,
            )
        except Stage2Error as exc:
            return GenerationResult(
                mechanism="claude-p",
                return_code=None,
                technical_error=exc.kind,
                technical_detail=exc.message,
            )

        command = [
            *launch.command_prefix,
            str(executable),
            "-p",
            request.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            *ALLOWED_TOOLS,
            "--model",
            REQUESTED_MODEL,
        ]
        try:
            result = self._run(
                command,
                cwd=request.detached.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return GenerationResult(
                mechanism="claude-p",
                stdout=_bytes(exc.stdout),
                stderr=_bytes(exc.stderr),
                return_code=None,
                technical_error="generator-timeout",
                technical_detail="claude generation timed out",
            )
        except OSError:
            return GenerationResult(
                mechanism="claude-p",
                return_code=None,
                technical_error="spawn-failure",
                technical_detail="claude generation could not be spawned",
            )
        return GenerationResult(
            mechanism="claude-p",
            stdout=_bytes(result.stdout),
            stderr=_bytes(result.stderr),
            return_code=result.returncode,
        )


def _bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")


def _is_utf8(value: bytes | str | None) -> bool:
    try:
        _bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def _sanitized_text(
    raw: bytes | str,
    limit: int,
    replacements: Mapping[str, str],
    *,
    normalize_whitespace: bool = False,
) -> str:
    """Redact a complete value before applying its UTF-8 evidence byte bound."""

    value = _bytes(raw).decode("utf-8", errors="replace")
    for original, replacement in sorted(replacements.items(), key=lambda item: -len(item[0])):
        if original:
            value = value.replace(original, replacement)
    if normalize_whitespace:
        value = " ".join(value.split())
    bounded = value.encode("utf-8")[:limit]
    text = bounded.decode("utf-8", errors="ignore")
    return text.rstrip() if normalize_whitespace else text


def _bounded_text(raw: bytes, limit: int, replacements: Mapping[str, str]) -> tuple[str, bool]:
    return _sanitized_text(raw, limit, replacements), len(raw) > limit


def _sanitized_error_detail(raw: bytes | str, replacements: Mapping[str, str]) -> str:
    return (
        _sanitized_text(raw, 1024, replacements, normalize_whitespace=True)
        or "stage-2 technical failure"
    )


def _raw_ref(prepared: PreparedPacket) -> tuple[str, Path]:
    match = _RAW_REF.fullmatch(prepared.manifest_ref.replace("packet/manifest.json", "raw-output.json"))
    if match is None or match.group(2) != prepared.attempt_id:
        raise Stage2Error("attempt-identity-invalid", "packet manifest ref cannot identify raw evidence")
    return match.group(0), prepared.attempt_dir / "raw-output.json"


def write_raw_output(
    prepared: PreparedPacket,
    research_root: str | Path,
    result: GenerationResult,
    technical_error: str | None,
    *,
    detached: DetachedPacket | None = None,
    technical_detail: str | None = None,
    generator: Mapping[str, Any] | None = None,
    phase: str = "generator",
) -> RawOutputRef:
    root = Path(research_root).expanduser().resolve()
    if not prepared.attempt_dir.resolve().is_relative_to(root):
        raise Stage2Error("attempt-identity-invalid", "attempt directory is outside the research root")
    ref, path = _raw_ref(prepared)
    replacements = {str(root): "<research-root>"}
    if detached is not None:
        replacements[str(detached.root)] = "<detached-packet>"
        replacements[str(detached.home_dir)] = "<temporary-home>"
    stdout, stdout_truncated = _bounded_text(result.stdout, MAX_STDOUT_BYTES, replacements)
    stderr, stderr_truncated = _bounded_text(result.stderr, MAX_STDERR_BYTES, replacements)
    generation_detail = None
    if result.technical_detail is not None:
        generation_detail = _sanitized_error_detail(result.technical_detail, replacements)
    final_detail = None
    if technical_detail is not None:
        final_detail = _sanitized_error_detail(technical_detail, replacements)
    if result.technical_error is not None and final_detail is not None:
        generation_detail = final_detail
    if phase not in {"pre-generator", "generator"}:
        raise Stage2Error("raw-envelope-invalid", "raw generation phase is invalid")
    envelope = {
        "format": RAW_OUTPUT_FORMAT,
        "generation": {
            "phase": phase,
            "mechanism": result.mechanism,
            "stdout_utf8": _is_utf8(result.stdout),
            "synthetic_session_id": result.synthetic_session_id,
            "technical_error": result.technical_error,
            "technical_detail": generation_detail,
        },
        "generator": None if generator is None else dict(generator),
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "stderr": stderr,
        "stderr_truncated": stderr_truncated,
        "return_code": result.return_code,
        "technical_error": technical_error,
        "technical_detail": final_detail,
    }
    content = _canonical_bytes(envelope)
    _write_exclusive(path, content)
    return RawOutputRef(ref, hashlib.sha256(content).hexdigest())


def write_decision_output(
    prepared: PreparedPacket,
    research_root: str | Path,
    *,
    packet: Mapping[str, Any],
    template: Mapping[str, str],
    generator: Mapping[str, Any],
    decision: ValidatedDecision,
    raw_output: RawOutputRef,
    error_kind: str | None,
) -> RawOutputRef:
    """Write one canonical, no-overwrite attempt-local decision document."""

    root = Path(research_root).expanduser().resolve()
    if not prepared.attempt_dir.resolve().is_relative_to(root):
        raise Stage2Error("attempt-identity-invalid", "attempt directory is outside the research root")
    ref = prepared.manifest_ref.replace("packet/manifest.json", "decision.json")
    if not ref.startswith(f"var/cgate/MR-") or not ref.endswith("/decision.json"):
        raise Stage2Error("attempt-identity-invalid", "packet manifest ref cannot identify decision evidence")
    content = _canonical_bytes(
        {
            "format": DECISION_FORMAT,
            "attempt_id": prepared.attempt_id,
            "packet": dict(packet),
            "template": dict(template),
            "generator": dict(generator),
            "verdict": decision.verdict,
            "note": decision.note,
            "observations": list(decision.observations),
            "raw_output": {"ref": raw_output.ref, "sha256": raw_output.sha256},
            "error_kind": error_kind,
        }
    )
    _write_exclusive(prepared.attempt_dir / "decision.json", content)
    return RawOutputRef(ref, hashlib.sha256(content).hexdigest())


def _generator_failure(kind: str, message: str, *, actual_model: str | None = None, session: str | None = None) -> tuple[None, dict[str, Any], Stage2Error]:
    error = Stage2Error(kind, message)
    return None, {
        "mechanism": "claude-p",
        "status": "technical-failure",
        "requested_model": REQUESTED_MODEL,
        "actual_model": actual_model,
        "session_ref": session,
        "error": {"kind": kind, "message": message},
    }, error


def _parse_generation(result: GenerationResult) -> tuple[str | None, dict[str, Any], Stage2Error | None]:
    if result.mechanism == "injected-synthetic":
        session = result.synthetic_session_id
        if not isinstance(session, str) or not session.strip():
            return _generator_failure("synthetic-session-missing", "injected generation requires a synthetic session id")
        provenance = {
            "mechanism": "injected-synthetic",
            "status": "synthetic",
            "requested_model": None,
            "actual_model": "<synthetic>",
            "session_ref": session,
            "error": None,
        }
        if result.technical_error is not None:
            return None, provenance, Stage2Error(result.technical_error, result.technical_detail or "injected generation failed")
        if result.return_code not in (0, None):
            return None, provenance, Stage2Error("nonzero-exit", f"injected generation returned status {result.return_code}")
        if not result.stdout.strip():
            return None, provenance, Stage2Error("empty-stdout", "generator returned empty stdout")
        try:
            return result.stdout.decode("utf-8", errors="strict"), provenance, None
        except UnicodeDecodeError:
            return None, provenance, Stage2Error("model-body-malformed", "generator body is not UTF-8")

    if result.mechanism != "claude-p":
        return _generator_failure("generator-mechanism-invalid", "generator returned an unknown mechanism")
    if result.technical_error is not None:
        return _generator_failure(result.technical_error, result.technical_detail or "real generator failed")
    if result.return_code is None:
        return _generator_failure("spawn-failure", "real generator returned no process status")
    if result.return_code != 0:
        return _generator_failure("nonzero-exit", f"claude generation returned status {result.return_code}")
    if not result.stdout.strip():
        return _generator_failure("empty-stdout", "claude generation returned empty stdout")
    try:
        envelope = json.loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _generator_failure("claude-envelope-malformed", "claude stdout is not one UTF-8 JSON value")
    if not isinstance(envelope, dict):
        return _generator_failure("claude-envelope-malformed", "claude JSON envelope must be an object")
    usage = envelope.get("modelUsage")
    model_keys = [
        key
        for key in usage
        if isinstance(key, str)
        and key.strip() == key
        and 0 < len(key) <= 512
        and key.isprintable()
    ] if isinstance(usage, dict) else []
    session = envelope.get("session_id")
    observed_session = (
        session
        if isinstance(session, str)
        and session.strip() == session
        and 0 < len(session) <= 512
        and session.isprintable()
        else None
    )
    if len(model_keys) != 1 or not isinstance(usage, dict) or len(usage) != 1:
        return _generator_failure(
            "model-identity-invalid",
            "claude envelope must contain exactly one non-empty modelUsage key",
            session=observed_session,
        )
    actual_model = model_keys[0]
    if observed_session is None:
        return _generator_failure(
            "session-identity-missing",
            "claude envelope has no non-empty session id",
            actual_model=actual_model,
        )
    if envelope.get("is_error") is not False or envelope.get("subtype") != "success":
        return _generator_failure(
            "claude-reported-failure",
            "claude envelope did not report success",
            actual_model=actual_model,
            session=observed_session,
        )
    body = envelope.get("result")
    if not isinstance(body, str) or not body.strip():
        return _generator_failure(
            "empty-model-body",
            "claude envelope contains no non-empty result body",
            actual_model=actual_model,
            session=observed_session,
        )
    return body, {
        "mechanism": "claude-p",
        "status": "success",
        "requested_model": REQUESTED_MODEL,
        "actual_model": actual_model,
        "session_ref": observed_session,
        "error": None,
    }, None


def _failure_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    screen = record.get("screen")
    rows = screen.get("results") if isinstance(screen, dict) else None
    if not isinstance(rows, list):
        raise Stage2Error("condition-evidence-invalid", "merge record has no screen result rows")
    failed = [row for row in rows if isinstance(row, dict) and row.get("result") == "fail"]
    if not failed or any(not isinstance(row.get("check"), str) or not isinstance(row.get("inputs"), dict) for row in failed):
        raise Stage2Error("condition-evidence-invalid", "screen failure conditions are malformed")
    return failed


def _contains_unblock(note: str) -> bool:
    lowered = note.casefold()
    return any(word in lowered for word in _UNBLOCK_WORDS)


def _note_names(note: str, values: Sequence[str]) -> bool:
    lowered = note.casefold()
    return bool(values) and all(value.casefold() in lowered for value in values)


def _ids(inputs: Mapping[str, Any], field: str, nested: str) -> list[str]:
    rows = inputs.get(field)
    if not isinstance(rows, list):
        return []
    found: list[str] = []
    for row in rows:
        value = row.get(nested) if isinstance(row, dict) else None
        if isinstance(value, str) and value:
            found.append(value)
    return found


def _binding_outcome(rules_fired: Sequence[Mapping[str, Any]]) -> str | None:
    binding = [
        rule.get("outcome")
        for rule in rules_fired
        if rule.get("rule_id") in {"R-OVERLAP-SEQ", "R-SETTLE-HOLD", "R-CONSOLIDATE"}
    ]
    if not binding:
        return None
    if len(binding) != 1 or not isinstance(binding[0], str):
        raise Stage2Error("condition-evidence-invalid", "binding stage-2 rules are ambiguous")
    return binding[0]


def _reserved_escalation(
    note: str,
    observation_refs: set[str],
    manifest: Mapping[str, Any],
    prepared: PreparedPacket,
) -> bool:
    lowered = note.casefold()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not observation_refs:
        return False
    kinds_by_ref = {
        row.get("packet_ref"): row.get("artifact_kind")
        for row in artifacts
        if isinstance(row, dict)
    }
    anchored_kinds = {kinds_by_ref.get(ref) for ref in observation_refs}
    guidance = (
        "guidance" in lowered
        and "promotion" in lowered
        and ("actor-visible" in lowered or "pattern-scope" in lowered or "pattern scope" in lowered)
        and bool(anchored_kinds & {"candidate-report", "candidate-node", "source-dispatch"})
    )
    budget = (
        all(word in lowered for word in ("directive", "budget", "growth", "core"))
        and ("no consolidation" in lowered or "without consolidation" in lowered)
        and "candidate-report" in anchored_kinds
    )
    operative_refs: set[str] = set()
    context_entry = next(
        (row for row in artifacts if isinstance(row, dict) and row.get("artifact_kind") == "packet-context"),
        None,
    )
    if isinstance(context_entry, dict):
        context_ref = context_entry.get("packet_ref")
        try:
            context_bytes = (prepared.attempt_dir / str(context_ref)).read_bytes()
            if hashlib.sha256(context_bytes).hexdigest() == context_entry.get("sha256"):
                context = json.loads(context_bytes.decode("utf-8", errors="strict"))
                operative_id = context.get("operative_merge_schedule_pc_decision")
                if isinstance(operative_id, str):
                    operative_refs = {
                        row.get("packet_ref")
                        for row in artifacts
                        if isinstance(row, dict)
                        and row.get("artifact_kind") == "pc-decision"
                        and row.get("source_ref") == f"tier1/decision-log/{operative_id}.json"
                        and isinstance(row.get("packet_ref"), str)
                    }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            operative_refs = set()
    pc_disagreement = (
        ("gate" in lowered and ("pc" in lowered or "program chair" in lowered) and ("disagree" in lowered or "conflict" in lowered))
        and bool(observation_refs & operative_refs)
    )
    return guidance or budget or pc_disagreement


def _eligible(
    verdict: str,
    note: str,
    observations: Sequence[dict[str, Any]],
    record: Mapping[str, Any],
    rules_fired: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    prepared: PreparedPacket,
) -> None:
    if verdict == "land":
        raise Stage2Error("stage2-land-illegal", "exact land is never eligible after a failed screen")
    refs = {
        anchor["ref"]
        for observation in observations
        for anchor in observation["anchors"]
    }
    if verdict == "escalate-to-user":
        if not _reserved_escalation(note, refs, manifest, prepared):
            raise Stage2Error("reserved-escalation-ineligible", "escalate-to-user lacks a matching reserved case and packet anchor")
        return
    if verdict == "escalate-stuck":
        lowered = note.casefold()
        if not any(word in lowered for word in _STUCK_WORDS) or not any(
            subject in lowered for subject in _STUCK_SUBJECTS
        ):
            raise Stage2Error("stuck-note-ineligible", "escalate-stuck note does not name missing or broken evidence")
        return

    failed = _failure_rows(record)
    names = [row["check"] for row in failed]
    binding = _binding_outcome(rules_fired)
    if binding is not None:
        expected_check = (
            "scope-overlap"
            if binding.startswith("land-after-")
            else "settlement-completeness"
            if binding == "hold"
            else "queue-adjacency"
            if binding == "consolidate-first"
            else None
        )
        if len(failed) != 1 or expected_check is None or failed[0]["check"] != expected_check:
            raise Stage2Error("condition-evidence-invalid", "binding preset contradicts the failed condition")
        if verdict != binding:
            raise Stage2Error("verdict-condition-ineligible", f"binding preset requires {binding}")
        binding_ids: list[str] = []
        if verdict == "hold":
            binding_ids = _ids(failed[0]["inputs"], "overdue_rows", "row_id")
            if not _contains_unblock(note) or not _note_names(note, binding_ids):
                raise Stage2Error("hold-note-ineligible", "binding hold note does not name every overdue row and unblock")
        elif verdict == "consolidate-first":
            binding_ids = _ids(failed[0]["inputs"], "pending_records", "record_id")
            if binding_ids and not _note_names(note, binding_ids):
                raise Stage2Error("consolidate-note-ineligible", "binding consolidate note does not name every pending record")
        return
    if verdict.startswith("land-after-") or verdict in {"consolidate-first"}:
        raise Stage2Error("verdict-condition-ineligible", "verdict requires a matching binding preset")

    if len(failed) >= 2:
        if verdict == "hold":
            if not _contains_unblock(note):
                raise Stage2Error("hold-note-ineligible", "hold note does not name an unblock condition")
            return
        if verdict == "bounce-for-surface-rework" and ({"scope-overlap", "surface-budget"} & set(names)):
            other = [name for name in names if name not in {"scope-overlap", "surface-budget"}]
            if other and not _note_names(note, other):
                raise Stage2Error("bounce-note-ineligible", "bounce note does not name every other failure")
            return
        raise Stage2Error("verdict-condition-ineligible", "verdict is not eligible for multiple failures")

    row = failed[0]
    check = row["check"]
    inputs = row["inputs"]
    if check == "surface-budget":
        if verdict == "bounce-for-surface-rework":
            return
        surfaces = list((inputs.get("candidate") or {}).get("scope", {}).get("surfaces", [])) if isinstance(inputs.get("candidate"), dict) else []
        if verdict == "hold" and _contains_unblock(note) and _note_names(note, [value for value in surfaces if isinstance(value, str)]):
            return
    elif check == "watch-debt":
        row_ids = _ids(inputs, "overlapping_watch_rows", "row_id")
        if verdict == "hold" and _contains_unblock(note) and _note_names(note, row_ids):
            return
    elif check == "scope-overlap":
        if verdict == "bounce-for-surface-rework":
            return
        collision_ids = _ids(inputs, "collisions", "record_id")
        if verdict == "hold" and _contains_unblock(note) and _note_names(note, collision_ids):
            return
    elif check == "queue-adjacency":
        pending_ids = _ids(inputs, "pending_records", "record_id")
        if verdict == "hold" and _contains_unblock(note) and _note_names(note, pending_ids):
            return
    raise Stage2Error("verdict-condition-ineligible", f"{verdict} is not eligible for {check}")


def validate_model_body(
    body: str,
    *,
    prepared: PreparedPacket,
    record: Mapping[str, Any],
    rules_fired: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> ValidatedDecision:
    """Parse one exact model object, anchor observations, and enforce CG-P4 eligibility."""

    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_MODEL_BODY_BYTES:
        raise Stage2Error("model-body-too-large", "model body exceeds the bounded validation limit")
    try:
        value = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise Stage2Error("model-body-malformed", "model body is not exactly one JSON value") from exc
    if not isinstance(value, dict) or set(value) != {"verdict", "note", "observations"}:
        raise Stage2Error("model-body-fields-invalid", "model body must have exactly verdict, note, and observations")
    verdict = value["verdict"]
    note = value["note"]
    raw_observations = value["observations"]
    if not isinstance(verdict, str) or (
        verdict not in VERDICTS and _LAND_AFTER.fullmatch(verdict) is None
    ):
        raise Stage2Error("model-verdict-invalid", "model verdict is outside the seven-token vocabulary")
    if verdict == "land-after-X":
        raise Stage2Error("model-verdict-invalid", "land-after-X requires a concrete non-empty suffix")
    if not isinstance(note, str) or not note.strip():
        raise Stage2Error("model-note-empty", "model note must be non-empty")
    if len(note) > MAX_NOTE_CHARACTERS:
        raise Stage2Error("model-note-too-large", "model note exceeds the bounded validation limit")
    if not isinstance(raw_observations, list):
        raise Stage2Error("model-observations-invalid", "model observations must be an array")
    if len(raw_observations) > MAX_OBSERVATIONS:
        raise Stage2Error("model-observations-too-large", "model observations exceed the bounded validation limit")
    hashes = manifest.get("input_hashes")
    if not isinstance(hashes, dict) or hashes != prepared.input_hashes:
        raise Stage2Error("detached-manifest-mismatch", "validated manifest hashes differ from prepared packet")
    observations: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_observations):
        if not isinstance(raw, dict) or set(raw) != {"text", "artifact_refs"}:
            raise Stage2Error("model-observation-fields-invalid", f"observation {index} has wrong fields")
        text = raw["text"]
        refs = raw["artifact_refs"]
        if not isinstance(text, str) or not text.strip():
            raise Stage2Error("model-observation-text-empty", f"observation {index} text is empty")
        if len(text) > MAX_OBSERVATION_CHARACTERS:
            raise Stage2Error("model-observation-text-too-large", f"observation {index} text exceeds the bounded validation limit")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
            raise Stage2Error("model-observation-unanchored", f"observation {index} has no artifact refs")
        if len(refs) != len(set(refs)):
            raise Stage2Error("model-observation-ref-invalid", f"observation {index} repeats an artifact ref")
        bad = [ref for ref in refs if ref not in hashes]
        if bad:
            raise Stage2Error("model-observation-ref-invalid", f"observation {index} cites an unknown artifact ref")
        observations.append(
            {
                "text": text,
                "anchors": [{"ref": ref, "sha256": hashes[ref]} for ref in refs],
            }
        )
    _eligible(verdict, note, observations, record, rules_fired, manifest, prepared)
    return ValidatedDecision(verdict, note, tuple(observations))


_RAW_EVIDENCE_FIELDS = frozenset(
    {
        "format",
        "generation",
        "generator",
        "stdout",
        "stdout_truncated",
        "stderr",
        "stderr_truncated",
        "return_code",
        "technical_error",
        "technical_detail",
    }
)
_RAW_GENERATION_FIELDS = frozenset(
    {
        "phase",
        "mechanism",
        "stdout_utf8",
        "synthetic_session_id",
        "technical_error",
        "technical_detail",
    }
)
_GENERATOR_FIELDS = frozenset(
    {
        "mechanism",
        "status",
        "requested_model",
        "actual_model",
        "session_ref",
        "error",
    }
)
_RAW_REDACTION_SENTINELS = (
    "<research-root>",
    "<detached-packet>",
    "<temporary-home>",
)


def _verified_generator(value: Any, generation: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _GENERATOR_FIELDS:
        raise Stage2Error("raw-generator-invalid", "raw generator provenance has the wrong fields")
    mechanism = value.get("mechanism")
    status = value.get("status")
    requested = value.get("requested_model")
    actual = value.get("actual_model")
    session = value.get("session_ref")
    error = value.get("error")
    if mechanism not in {"claude-p", "injected-synthetic"}:
        raise Stage2Error("raw-generator-invalid", "raw generator mechanism is invalid")
    for label, item in (("requested_model", requested), ("actual_model", actual), ("session_ref", session)):
        if item is not None and (not isinstance(item, str) or not item.strip()):
            raise Stage2Error("raw-generator-invalid", f"raw generator {label} is invalid")
    if status == "synthetic":
        expected = {
            "mechanism": "injected-synthetic",
            "status": "synthetic",
            "requested_model": None,
            "actual_model": "<synthetic>",
            "session_ref": generation.get("synthetic_session_id"),
            "error": None,
        }
        if (
            value != expected
            or generation.get("phase") != "generator"
            or generation.get("mechanism") != "injected-synthetic"
        ):
            raise Stage2Error("raw-generator-invalid", "synthetic provenance contradicts raw generation facts")
    elif status == "success":
        if (
            mechanism != "claude-p"
            or generation.get("mechanism") != "claude-p"
            or requested != REQUESTED_MODEL
            or not isinstance(actual, str)
            or not isinstance(session, str)
            or error is not None
            or generation.get("phase") != "generator"
        ):
            raise Stage2Error("raw-generator-invalid", "successful provenance is incomplete")
    elif status == "technical-failure":
        if not isinstance(error, dict) or set(error) != {"kind", "message"}:
            raise Stage2Error("raw-generator-invalid", "technical-failure provenance has no exact error")
        if any(not isinstance(error.get(field), str) or not error[field] for field in ("kind", "message")):
            raise Stage2Error("raw-generator-invalid", "technical-failure provenance error is invalid")
        if mechanism != "claude-p":
            raise Stage2Error("raw-generator-invalid", "synthetic provenance cannot claim technical-failure status")
    else:
        raise Stage2Error("raw-generator-invalid", "raw generator status is invalid")
    return dict(value)


def verify_stage2_raw_evidence(
    raw: Mapping[str, Any],
    *,
    prepared: PreparedPacket,
    record: Mapping[str, Any],
    rules_fired: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct one stage-2 result from raw evidence without a model call.

    The returned fields are the only generator/decision fields a finalizer may
    accept.  A successful raw result is reparsed, re-anchored, and checked for
    CG-P4 eligibility.  Every technical path deterministically reconstructs the
    stuck decision, so mutable prepared/decision documents cannot override the
    raw evidence.
    """

    if not isinstance(raw, Mapping) or set(raw) != _RAW_EVIDENCE_FIELDS:
        raise Stage2Error("raw-envelope-invalid", "raw evidence has the wrong fields")
    if raw.get("format") != RAW_OUTPUT_FORMAT:
        raise Stage2Error("raw-envelope-invalid", "raw evidence format is invalid")
    generation = raw.get("generation")
    if not isinstance(generation, dict) or set(generation) != _RAW_GENERATION_FIELDS:
        raise Stage2Error("raw-envelope-invalid", "raw generation facts have the wrong fields")
    if generation.get("phase") not in {"pre-generator", "generator"}:
        raise Stage2Error("raw-envelope-invalid", "raw generation phase is invalid")
    if not isinstance(generation.get("mechanism"), str) or not generation["mechanism"]:
        raise Stage2Error("raw-envelope-invalid", "raw generation mechanism is invalid")
    if type(generation.get("stdout_utf8")) is not bool:
        raise Stage2Error("raw-envelope-invalid", "raw stdout encoding fact is invalid")
    for field in ("synthetic_session_id", "technical_error", "technical_detail"):
        value = generation.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise Stage2Error("raw-envelope-invalid", f"raw generation {field} is invalid")
    if not isinstance(raw.get("stdout"), str) or not isinstance(raw.get("stderr"), str):
        raise Stage2Error("raw-envelope-invalid", "raw stdout/stderr must be text")
    if type(raw.get("stdout_truncated")) is not bool or type(raw.get("stderr_truncated")) is not bool:
        raise Stage2Error("raw-envelope-invalid", "raw truncation flags must be booleans")
    return_code = raw.get("return_code")
    if return_code is not None and (not isinstance(return_code, int) or isinstance(return_code, bool)):
        raise Stage2Error("raw-envelope-invalid", "raw return code is invalid")
    final_kind = raw.get("technical_error")
    final_detail = raw.get("technical_detail")
    if (final_kind is None) != (final_detail is None) or (
        final_kind is not None
        and (
            not isinstance(final_kind, str)
            or not final_kind
            or not isinstance(final_detail, str)
            or not final_detail
        )
    ):
        raise Stage2Error("raw-envelope-invalid", "raw final technical error is incomplete")

    generator = _verified_generator(raw.get("generator"), generation)
    if generation["phase"] == "pre-generator":
        if (
            generation.get("mechanism") != "claude-p"
            or generation.get("stdout_utf8") is not True
            or generation.get("synthetic_session_id") is not None
            or raw.get("stdout") != ""
            or raw.get("stderr") != ""
            or raw.get("return_code") is not None
            or final_kind is None
            or generation.get("technical_error") != final_kind
            or generation.get("technical_detail") != final_detail
            or generator
            != {
                "mechanism": "claude-p",
                "status": "technical-failure",
                "requested_model": None,
                "actual_model": None,
                "session_ref": None,
                "error": {"kind": final_kind, "message": final_detail},
            }
        ):
            raise Stage2Error("raw-generator-invalid", "pre-generator failure evidence is contradictory")
        error = Stage2Error(final_kind, final_detail)
        decision = _stuck(error)
    else:
        validation_error: Stage2Error | None = None
        validated: ValidatedDecision | None = None
        if raw.get("stdout_truncated") or raw.get("stderr_truncated"):
            validation_error = Stage2Error(
                "generator-output-truncated",
                "generator output exceeded bounded raw evidence limits",
            )
            if (
                generation.get("mechanism") == "injected-synthetic"
                and isinstance(generation.get("synthetic_session_id"), str)
                and generation["synthetic_session_id"].strip()
            ):
                expected_generator = {
                    "mechanism": "injected-synthetic",
                    "status": "synthetic",
                    "requested_model": None,
                    "actual_model": "<synthetic>",
                    "session_ref": generation["synthetic_session_id"],
                    "error": None,
                }
            else:
                _body, expected_generator, _ignored = _generator_failure(
                    validation_error.kind, validation_error.message
                )
            if generator != expected_generator:
                raise Stage2Error("raw-generator-invalid", "truncation provenance is contradictory")
        elif generation.get("stdout_utf8") is not True:
            if generation.get("mechanism") == "injected-synthetic":
                validation_error = Stage2Error(
                    "model-body-malformed", "generator body is not UTF-8"
                )
                expected_generator = {
                    "mechanism": "injected-synthetic",
                    "status": "synthetic",
                    "requested_model": None,
                    "actual_model": "<synthetic>",
                    "session_ref": generation.get("synthetic_session_id"),
                    "error": None,
                }
            elif generation.get("mechanism") == "claude-p":
                _body, expected_generator, validation_error = _generator_failure(
                    "claude-envelope-malformed",
                    "claude stdout is not one UTF-8 JSON value",
                )
            else:
                _body, expected_generator, validation_error = _generator_failure(
                    "generator-mechanism-invalid",
                    "generator returned an unknown mechanism",
                )
            if generator != expected_generator:
                raise Stage2Error("raw-generator-invalid", "encoding-failure provenance is contradictory")
        else:
            reconstructed = GenerationResult(
                mechanism=generation["mechanism"],
                stdout=raw["stdout"].encode("utf-8"),
                stderr=raw["stderr"].encode("utf-8"),
                return_code=return_code,
                technical_error=generation.get("technical_error"),
                technical_detail=generation.get("technical_detail"),
                synthetic_session_id=generation.get("synthetic_session_id"),
            )
            body, parsed_generator, parsed_error = _parse_generation(reconstructed)
            identity_unsafe = parsed_error is None and _protected_path_in(
                parsed_generator, _RAW_REDACTION_SENTINELS
            )
            if identity_unsafe:
                validation_error = Stage2Error(
                    "generator-identity-unsafe",
                    "observed generator identity contains a protected filesystem path",
                )
                _body, expected_generator, _ignored = _generator_failure(
                    validation_error.kind, validation_error.message
                )
                if generator != expected_generator:
                    raise Stage2Error("raw-generator-invalid", "unsafe identity provenance is contradictory")
            else:
                if parsed_generator != generator:
                    raise Stage2Error("raw-generator-invalid", "raw generator provenance does not reconstruct")
                validation_error = parsed_error
                if validation_error is None and body is not None:
                    try:
                        validated = validate_model_body(
                            body,
                            prepared=prepared,
                            record=record,
                            rules_fired=rules_fired,
                            manifest=manifest,
                        )
                        if _protected_path_in(
                            {"note": validated.note, "observations": validated.observations},
                            _RAW_REDACTION_SENTINELS,
                        ):
                            raise Stage2Error(
                                "model-body-unsafe",
                                "model body contains a protected filesystem path",
                            )
                    except Stage2Error as exc:
                        validation_error = exc

        if final_kind is None:
            if validation_error is not None or validated is None:
                raise Stage2Error("raw-decision-invalid", "successful raw evidence does not reconstruct a decision")
            decision = validated
        else:
            if validation_error is None or (
                validation_error.kind != final_kind
                or validation_error.message != final_detail
            ):
                raise Stage2Error("raw-decision-invalid", "raw technical failure does not match reconstruction")
            decision = _stuck(validation_error)

    return {
        "generator": generator,
        "verdict": decision.verdict,
        "note": decision.note,
        "observations": list(decision.observations),
        "error_kind": final_kind,
    }


def _prompt(rules_fired: Sequence[Mapping[str, Any]]) -> str:
    try:
        template = review_prompt_bytes().decode("utf-8", errors="strict")
    except PacketError as exc:
        raise Stage2Error(exc.kind, exc.message) from exc
    except UnicodeDecodeError as exc:
        raise Stage2Error("prompt-invalid", "frozen review prompt is not UTF-8") from exc
    binding = _binding_outcome(rules_fired)
    if binding is None:
        return template
    return (
        template
        + "\n\nMechanically binding CG-P4 preset for this packet: "
        + binding
        + ". Return that exact token unless packet evidence supports escalate-stuck "
        "or one of the three reserved escalate-to-user cases.\n"
    )


def _packet_metadata(prepared: PreparedPacket) -> dict[str, Any]:
    return {
        "manifest_ref": prepared.manifest_ref,
        "manifest_sha256": prepared.manifest_sha256,
        "artifact_refs": list(prepared.artifact_refs),
        "input_hashes": dict(prepared.input_hashes),
    }


def _stuck(error: Stage2Error) -> ValidatedDecision:
    return ValidatedDecision(
        "escalate-stuck",
        f"Stage-2 preparation failed: {error.kind}; {error.message}",
        (),
    )


def _sanitized_error(
    error: Stage2Error,
    research_root: Path,
    detached: DetachedPacket | None,
) -> Stage2Error:
    replacements = {str(research_root.resolve()): "<research-root>"}
    if detached is not None:
        replacements[str(detached.root)] = "<detached-packet>"
        replacements[str(detached.home_dir)] = "<temporary-home>"
    message = _sanitized_error_detail(error.message, replacements)
    return Stage2Error(error.kind, message)


def _protected_path_in(value: Any, protected_paths: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(path and path in value for path in protected_paths)
    if isinstance(value, Mapping):
        return any(
            _protected_path_in(key, protected_paths)
            or _protected_path_in(item, protected_paths)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_protected_path_in(item, protected_paths) for item in value)
    return False


def run_stage2(
    prepared: PreparedPacket,
    research_root: str | Path,
    record: Mapping[str, Any],
    rules_fired: Sequence[Mapping[str, Any]],
    generator: Generator,
    *,
    temp_factory: Callable[..., str] = tempfile.mkdtemp,
) -> Stage2Preparation:
    """Run one detached stage-2 attempt and write exactly one raw envelope."""

    detached: DetachedPacket | None = None
    raw_result = GenerationResult(
        mechanism="claude-p",
        return_code=None,
        technical_error="preparation-failure",
    )
    provenance: dict[str, Any] = {
        "mechanism": "claude-p",
        "status": "technical-failure",
        "requested_model": None,
        "actual_model": None,
        "session_ref": None,
        "error": None,
    }
    error: Stage2Error | None = None
    decision: ValidatedDecision | None = None
    manifest: Mapping[str, Any] | None = None
    generation_phase = "pre-generator"
    generator_called = False
    try:
        detached = detach_packet(prepared, research_root, temp_factory=temp_factory)
        manifest = detached.manifest
        request = GenerationRequest(
            _prompt(rules_fired),
            detached,
            Path(research_root).expanduser().resolve(),
        )
        try:
            generator_called = True
            raw_result = generator.generate(request)
            generation_phase = "generator"
        except Exception:
            generation_phase = "generator"
            raw_result = GenerationResult(
                mechanism="claude-p",
                return_code=None,
                technical_error="generator-exception",
                technical_detail="generator raised an unexpected exception",
            )
        if not isinstance(raw_result, GenerationResult):
            raise Stage2Error("generator-result-invalid", "generator returned an invalid result object")
        if (
            len(_bytes(raw_result.stdout)) > MAX_STDOUT_BYTES
            or len(_bytes(raw_result.stderr)) > MAX_STDERR_BYTES
        ):
            body = None
            error = Stage2Error(
                "generator-output-truncated",
                "generator output exceeded bounded raw evidence limits",
            )
            if (
                raw_result.mechanism == "injected-synthetic"
                and isinstance(raw_result.synthetic_session_id, str)
                and raw_result.synthetic_session_id.strip()
            ):
                provenance = {
                    "mechanism": "injected-synthetic",
                    "status": "synthetic",
                    "requested_model": None,
                    "actual_model": "<synthetic>",
                    "session_ref": raw_result.synthetic_session_id,
                    "error": None,
                }
            else:
                _body, provenance, _ignored = _generator_failure(
                    error.kind, error.message
                )
        else:
            body, provenance, error = _parse_generation(raw_result)
        protected_paths = (
            str(Path(research_root).expanduser().resolve()),
            str(detached.root),
            str(detached.home_dir),
            *_RAW_REDACTION_SENTINELS,
        )
        if error is None and _protected_path_in(provenance, protected_paths):
            body, provenance, error = _generator_failure(
                "generator-identity-unsafe",
                "observed generator identity contains a protected filesystem path",
            )
        if error is None and body is not None:
            try:
                decision = validate_model_body(
                    body,
                    prepared=prepared,
                    record=record,
                    rules_fired=rules_fired,
                    manifest=manifest,
                )
                if _protected_path_in(
                    {"note": decision.note, "observations": decision.observations},
                    protected_paths,
                ):
                    raise Stage2Error(
                        "model-body-unsafe",
                        "model body contains a protected filesystem path",
                    )
            except Stage2Error as exc:
                error = exc
    except Stage2Error as exc:
        generation_phase = "generator" if generator_called else "pre-generator"
        error = exc
        raw_result = GenerationResult(
            mechanism="claude-p",
            return_code=None,
            technical_error=exc.kind,
            technical_detail=exc.message,
        )
        if generator_called:
            _body, provenance, _ignored = _generator_failure(exc.kind, exc.message)
        else:
            provenance = {
                "mechanism": "claude-p",
                "status": "technical-failure",
                "requested_model": None,
                "actual_model": None,
                "session_ref": None,
                "error": {"kind": exc.kind, "message": exc.message},
            }
    except Exception:
        generation_phase = "generator" if generator_called else "pre-generator"
        error = Stage2Error(
            "preparation-exception",
            "stage-2 preparation raised an unexpected technical exception",
        )
        raw_result = GenerationResult(
            mechanism="claude-p",
            return_code=None,
            technical_error=error.kind,
            technical_detail=error.message,
        )
        if generator_called:
            _body, provenance, _ignored = _generator_failure(error.kind, error.message)
        else:
            provenance = {
                "mechanism": "claude-p",
                "status": "technical-failure",
                "requested_model": None,
                "actual_model": None,
                "session_ref": None,
                "error": {"kind": error.kind, "message": error.message},
            }

    resolved_root = Path(research_root).expanduser().resolve()
    if error is not None:
        error = _sanitized_error(error, resolved_root, detached)
        decision = _stuck(error)
        if provenance.get("status") == "technical-failure":
            provenance = {
                **provenance,
                "error": {"kind": error.kind, "message": error.message},
            }
    if decision is None:
        error = Stage2Error("preparation-incomplete", "stage-2 preparation produced no decision")
        decision = _stuck(error)
        provenance = {
            "mechanism": "claude-p",
            "status": "technical-failure",
            "requested_model": None,
            "actual_model": None,
            "session_ref": None,
            "error": {"kind": error.kind, "message": error.message},
        }
    try:
        raw_ref = write_raw_output(
            prepared,
            research_root,
            raw_result,
            error.kind if error is not None else None,
            detached=detached,
            technical_detail=error.message if error is not None else None,
            generator=provenance,
            phase=generation_phase,
        )
        packet_metadata = _packet_metadata(prepared)
        template_metadata = {"name": PROMPT_NAME, "sha256": PROMPT_SHA256}
        decision_ref = write_decision_output(
            prepared,
            research_root,
            packet=packet_metadata,
            template=template_metadata,
            generator=provenance,
            decision=decision,
            raw_output=raw_ref,
            error_kind=error.kind if error is not None else None,
        )
    finally:
        if detached is not None:
            cleanup_detached(detached)
    return Stage2Preparation(
        attempt_id=prepared.attempt_id,
        packet=packet_metadata,
        template=template_metadata,
        generator=provenance,
        verdict=decision.verdict,
        note=decision.note,
        observations=decision.observations,
        raw_output=raw_ref,
        decision_output=decision_ref,
        error_kind=error.kind if error is not None else None,
    )


def prepare_technical_failure(
    prepared: PreparedPacket,
    research_root: str | Path,
    error: Stage2Error,
) -> Stage2Preparation:
    """Record a truthful pre-generator failure without invoking a generator."""

    resolved_root = Path(research_root).expanduser().resolve()
    sanitized = _sanitized_error(error, resolved_root, None)
    provenance = {
        "mechanism": "claude-p",
        "status": "technical-failure",
        "requested_model": None,
        "actual_model": None,
        "session_ref": None,
        "error": {"kind": sanitized.kind, "message": sanitized.message},
    }
    decision = _stuck(sanitized)
    raw_result = GenerationResult(
        mechanism="claude-p",
        return_code=None,
        technical_error=sanitized.kind,
        technical_detail=sanitized.message,
    )
    raw_ref = write_raw_output(
        prepared,
        resolved_root,
        raw_result,
        sanitized.kind,
        technical_detail=sanitized.message,
        generator=provenance,
        phase="pre-generator",
    )
    packet_metadata = _packet_metadata(prepared)
    template_metadata = {"name": PROMPT_NAME, "sha256": PROMPT_SHA256}
    decision_ref = write_decision_output(
        prepared,
        resolved_root,
        packet=packet_metadata,
        template=template_metadata,
        generator=provenance,
        decision=decision,
        raw_output=raw_ref,
        error_kind=sanitized.kind,
    )
    return Stage2Preparation(
        attempt_id=prepared.attempt_id,
        packet=packet_metadata,
        template=template_metadata,
        generator=provenance,
        verdict=decision.verdict,
        note=decision.note,
        observations=decision.observations,
        raw_output=raw_ref,
        decision_output=decision_ref,
        error_kind=sanitized.kind,
    )


__all__ = [
    "ALLOWED_TOOLS",
    "ClaudeGenerator",
    "DEFAULT_TIMEOUT_SECONDS",
    "DECISION_FORMAT",
    "DetachedPacket",
    "GenerationRequest",
    "GenerationResult",
    "Generator",
    "MacOSSandbox",
    "RAW_OUTPUT_FORMAT",
    "REQUESTED_MODEL",
    "RawOutputRef",
    "SandboxController",
    "SandboxError",
    "SandboxLaunch",
    "Stage2Error",
    "Stage2Preparation",
    "ValidatedDecision",
    "child_environment",
    "cleanup_detached",
    "detach_packet",
    "run_stage2",
    "prepare_technical_failure",
    "validate_model_body",
    "verify_stage2_raw_evidence",
    "write_raw_output",
    "write_decision_output",
]
