"""sandbox — the runtime-neutral seatbelt jail and strong-form read blinders.

The spawn chokepoint's containment leg. Spawned agents have auto-approved full tools and run
arbitrary code; this seatbelt is the STRUCTURAL blast-radius bound (§1.3), NOT the agent's
judgment. The existing write/secret/token floor is active in both production modes. ``observe``
reports broad reads while commissioning; ``enforce`` denies local reads by default and reopens
only the address/manifest/runtime-derived world. Network and tools stay open.

THREE seams the SECURITY-JAIL increment owns:

  * ``render_profile(containment) -> str`` — render the §2.3 ``.sb`` profile text from a RESOLVED
    ``containment_profile`` block (§2.5a). EVERY path is realpath-canonicalized (§2.4 — the #1
    silent-hole guard: a logical ``/tmp/X`` deny LEAKS while the ``/private/tmp/X`` realpath deny
    BLOCKS, so canonicalize UNCONDITIONALLY). Deny-all-then-allow write jail; keychain mach-deny;
    broad secret read-deny named set + pattern globs; cross-project read-deny; and the LAST-MATCH
    WORKROOT read re-allow (so the agent reads its OWN .env without un-denying siblings').

  * ``wrap(pane_argv, profile_path) -> list[str]`` — build the ``sandbox-exec -f <profile-file>
    <pane…>`` invocation that wraps the env-i pane command (§7.1: the seatbelt prefix is part of
    the detached pane's launch command-line — ``sandbox-exec -f <profile>.sb env -i <…> <binary>``).
    The env-i clean-slate isolator stays the pane head; sandbox-exec wraps the OUTSIDE.

  * ``cache_redirect_env(workroot) -> dict`` — the §2.3 tool-cache redirection env (NPM_CONFIG_CACHE
    etc. pointed INTO WORKROOT) so a real ``npm install`` / ``go mod`` / ``cargo fetch`` writes its
    per-user cache inside the jail instead of hard-failing EPERM on its very first fetch (§1.4).

§2.4 CANONICALIZATION is load-bearing and applied to EVERY templated path here — WORKROOT, TMPDIR,
CONFIG, HOME, every secret-deny path, extra_read_denies, extra_write_roots, READ_DENY_ROOT — via
``os.path.realpath`` before substitution. The traps are ``/tmp`` (-> ``/private/tmp``) and ``/var``
(-> ``/private/var``); the rule is canonicalize unconditionally because a single un-canonicalized
secret path is a silent leak.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The real seatbelt binary (root-owned 0755 on macOS 26.4; the substrate Apple's App Sandbox and
# Codex CLI use). Resolved once at import; ``wrap`` prefixes this.
SANDBOX_EXEC = (
    os.environ.get("HARNESSD_SANDBOX_EXEC")
    or shutil.which("sandbox-exec")
    or "/usr/bin/sandbox-exec"
)
RUNTIME_SCRATCH_DIRNAME = ".tmp"


def _canon(path: str) -> str:
    """Realpath-canonicalize a single templated path (§2.4 — the #1 silent-hole guard).

    ``/tmp`` -> ``/private/tmp``, ``/var`` -> ``/private/var``. The seatbelt matches the RESOLVED
    REAL path, not the symlink path, so a logical-path rule silently leaks (a deny that never
    fires) or silently over-denies (a write-allow that never matches). Canonicalize UNCONDITIONALLY
    — never trust the caller to have done it (the ``_containment`` test hands LOGICAL paths on
    purpose to prove this seam owns the canonicalization).
    """
    return os.path.realpath(str(path))


# ---------------------------------------------------------------------------
# The §2.3 per-node runtime scratch and tool-cache redirect paths.
# ---------------------------------------------------------------------------

def runtime_scratch_dir(workroot: str) -> str:
    """Return the one harness-provisioned per-node runtime scratch tree."""
    return os.path.join(str(workroot), RUNTIME_SCRATCH_DIRNAME)


def cache_redirect_env(workroot: str) -> dict[str, str]:
    """Return the §2.3 tool-cache redirection env (caches pointed INTO WORKROOT).

    Set at the chokepoint BEFORE launch so per-user package caches land INSIDE the jail instead of
    hard-failing EPERM. WITHOUT this a JS/Go/Rust/.NET build hard-fails on its VERY FIRST
    ``npm install`` / ``go mod`` / ``cargo`` fetch (VERIFIED §1.4); pip is the lucky cache-disabled
    exception but is redirected too for parity. The workroot is canonicalized so the env paths
    match the write-allow subpath the profile renders.
    """
    wr = _canon(workroot)
    return {
        "NPM_CONFIG_CACHE": f"{wr}/.cache/npm",
        "PIP_CACHE_DIR": f"{wr}/.cache/pip",
        "GOMODCACHE": f"{wr}/.cache/go",
        "GOCACHE": f"{wr}/.cache/gobuild",
        "CARGO_HOME": f"{wr}/.cargo",
        "YARN_CACHE_FOLDER": f"{wr}/.cache/yarn",
        "NUGET_PACKAGES": f"{wr}/.nuget",
    }


# ---------------------------------------------------------------------------
# resolve_containment — produce the §2.5a containment block from a node's real paths.
# This is the PRODUCTION SEAM the concrete spawn layer calls (the structural v1 chokepoint
# carries placeholder env, so it does not yet call this — see the OWED wiring note in the
# register: the real-boot / Phase-6 eval spawn resolves the real paths + calls this + attaches
# the block to the brief, which makes the adapter jail the pane).
# ---------------------------------------------------------------------------

def _node_workroot(node_address: str, runtime_root: str) -> str:
    """The node's WORKROOT — NESTED by path (``addressing.node_dir``), so a coordinator's WORKROOT is a
    PARENT dir of its children's WORKROOTs and `(allow file-write* (subpath WORKROOT))` covers the whole
    subtree it may seed (ARCHITECTURE.md:122 'creates child workspaces within it'). The `#seat` is not a
    path segment (it would break that nesting); seats share the node workspace."""
    from harnessd import addressing
    return str(addressing.node_dir(node_address, runtime_root))


def resolve_containment(
    node_address: str,
    *,
    runtime_root: str,
    config_dir: str,
    home: str | None = None,
    extra_read_denies=None,
    extra_write_roots=None,
    read_policy: dict | None = None,
    runtime: str = "claude-code",
) -> dict:
    """Resolve the §2.5a containment block for a node from its REAL paths (the v1 floor).

    WORKROOT = the node's own workspace subtree under the /runtime/ jail root; TMPDIR a per-node
    scratch under it; CONFIG the pinned config dir (CC's own state writes); HOME the user home (the
    secret-deny anchor); READ_DENY_ROOT = the whole /runtime/ root, so EVERY other node's subtree is
    read-denied while the WORKROOT re-allow (render_profile, last-match-wins) re-opens ONLY this
    node's own — the cross-project read-confidentiality floor (WORKSPACE-SCHEMA read graph; sibling
    published-contract + parent-chain reads are a deferred extra_read refinement). All paths are
    realpath-canonicalized by ``render_profile`` (§2.4); logical paths are fine to hand in here.
    """
    workroot = _node_workroot(node_address, str(runtime_root))
    block = {
        "WORKROOT": workroot,
        "TMPDIR": runtime_scratch_dir(workroot),
        "CONFIG": str(config_dir),
        "HOME": str(home or os.path.expanduser("~")),
        # A blinders policy owns the whole broad-read decision.  The legacy READ_DENY_ROOT remains
        # byte-for-byte for callers that have not entered this pass's observe/enforce modes.
        "READ_DENY_ROOT": "" if read_policy is not None else str(runtime_root),
        "extra_read_denies": list(extra_read_denies or []),
        "extra_write_roots": list(extra_write_roots or []),
        "read_policy": dict(read_policy) if read_policy is not None else None,
        "runtime": runtime,
        "allow_claude_home_write": runtime == "claude-code",
    }
    if read_policy is not None and runtime == "claude-code":
        block = finalize_runtime_state(block, runtime=runtime, state_root=str(config_dir))
    return block


def finalize_runtime_state(
    containment: dict,
    *,
    runtime: str,
    state_root: str,
    session_uuid: str | None = None,
) -> dict:
    """Bind a resolved policy to the exact runtime state home.

    Claude's state root is known at the chokepoint.  Codex calls this after it mints a unique worker
    CODEX_HOME, so the canonical refresh-token home and other seats never enter the child allowset.
    """
    from harnessd.spawn import blinders

    result = dict(containment)
    result["CONFIG"] = str(state_root)
    result["runtime"] = runtime
    result["allow_claude_home_write"] = runtime == "claude-code"
    policy = result.get("read_policy")
    if policy is not None:
        base = dict(policy)
        base["runtime_state_roots"] = []
        base["runtime_inner_denies"] = []
        base["runtime_inner_allows"] = []
        result["read_policy"] = blinders.with_runtime_state(
            base,
            runtime=runtime,
            state_root=str(state_root),
            session_uuid=session_uuid,
        )
    return result


# ---------------------------------------------------------------------------
# render_profile — the §2.3 .sb structure, every path realpath-canonicalized (§2.4).
# ---------------------------------------------------------------------------

# The §2.3 home-tree credential-store SUBPATH set (well beyond the old four dirs — the broad set
# the escape-path review found readable). Rendered as `(subpath "<HOME>/<rel>")` denies.
_SECRET_SUBPATH_RELS = (
    ".ssh",                 # NOTE: blocks known_hosts -> SSH-based dep fetch breaks by default (§1.4 / §2.3 option a)
    ".aws",                 # NOTE: blocks ~/.aws/config (non-secret region/profile) too — named limitation (§1.4)
    ".gnupg",
    "Library/Keychains",
    ".config/gh",
    ".config/gcloud",
    ".kube",
    ".docker",
    ".codex",
    ".gemini",
)

# The §2.3 home-tree credential-store LITERAL set (single-file credential stores + histories).
_SECRET_LITERAL_RELS = (
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".claude.json",
    ".claude/.credentials.json",
    ".zsh_history",
    ".bash_history",
)


def _logical_and_real(path: str) -> tuple[str, ...]:
    """Keep a declared logical symlink and its real target.

    Seatbelt checks the link lookup and the target read separately.  Existing deny paths still use
    ``_canon``; readable declarations need both forms or a declared symlink remains unreadable.
    """
    logical = os.path.abspath(os.path.normpath(str(path)))
    real = os.path.realpath(logical)
    return (logical,) if logical == real else (logical, real)


def _ancestors(path: str) -> tuple[str, ...]:
    current = Path(path)
    rows: list[str] = []
    while True:
        rows.append(str(current))
        if current.parent == current:
            break
        current = current.parent
    return tuple(reversed(rows))


def _regex_escape_path(path: str) -> str:
    # Escape regex metacharacters but leave spaces/hyphens readable in SBPL's #"...".
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", path)


def _render_blinders_read_policy(lines: list[str], policy: dict) -> None:
    from harnessd.spawn import blinders

    mode = policy.get("mode")
    if mode == blinders.OBSERVE:
        lines.append(";; --- BLINDERS OBSERVE: reads remain open and are reported by seatbelt ---")
        lines.append("(allow (with report) file-read*)")
        lines.append("")
        return
    if mode != blinders.ENFORCE:
        raise ValueError(f"unknown blinders mode in containment profile: {mode!r}")

    allow_literals: set[str] = set()
    allow_subpaths: set[str] = set()
    direct_surfaces: set[str] = set()
    for raw in policy.get("allow_literals") or []:
        for path in _logical_and_real(raw):
            # Exact declared files do not need their containing directories reopened.  Reopening
            # ancestors would let the seat list undeclared estate names even though file contents
            # stayed closed.  Subpath/surface roots below do need literal root/ancestry metadata.
            allow_literals.add(path)
    for raw in [
        *(policy.get("allow_subpaths") or []),
        *(policy.get("runtime_state_roots") or []),
    ]:
        for path in _logical_and_real(raw):
            allow_literals.update(_ancestors(path))
            allow_subpaths.add(path)
    for raw in policy.get("direct_surfaces") or []:
        for path in _logical_and_real(raw):
            allow_literals.update(_ancestors(path))
            direct_surfaces.add(path)

    lines.append(";; --- BLINDERS ENFORCE: local reads deny-by-default, reopen derived world ---")
    lines.append("(deny file-read*)")
    lines.append("(allow file-read*")
    for path in sorted(allow_literals):
        lines.append(f'  (literal {json.dumps(path)})')
    for path in sorted(allow_subpaths):
        lines.append(f'  (subpath {json.dumps(path)})')
    for path in sorted(direct_surfaces):
        regex = f"^{_regex_escape_path(path)}/[^/]+$"
        lines.append(f'  (regex #{json.dumps(regex)})')
    lines.append("  )")
    lines.append("")


def render_profile(containment: dict) -> str:
    """Render the §2.3 seatbelt ``.sb`` profile from a RESOLVED containment block (§2.5a).

    The containment block (§2.5a shape) carries the path-derived roots + resolved knob values:
    ``WORKROOT``, ``TMPDIR``, ``CONFIG``, ``HOME``, ``READ_DENY_ROOT``, ``extra_read_denies``,
    ``extra_write_roots``. EVERY path is realpath-canonicalized (§2.4) before substitution.

    The clause ORDER is load-bearing (seatbelt is last-match-wins; deny-all-then-allow for writes):
      1. ``(version 1)`` + ``(allow default)``     — network + system-lib + /etc reads open (§2.3)
      2. WRITE JAIL: ``(deny file-write*)`` THEN the allow-list (deny-all-then-allow)
      3. KEYCHAIN: ``(deny mach-lookup …)``         — the real keychain control (securityd is mach,
         not file IO; the file-read deny is irrelevant — §2.3 / §3.3)
      4. READ DENY: the broad secret named set (subpaths + literals) + extra_read_denies
      5. READ DENY: cross-project source (READ_DENY_ROOT), if given
      6. READ DENY: secret-pattern globs anywhere (**/.env, credentials/secrets, *.pem)
      7. ``(allow file-read* (subpath WORKROOT))`` — the LAST-MATCH re-allow that un-denies the
         agent's OWN .env/.pem WITHOUT un-denying siblings' (must be the LAST read rule).
    """
    workroot = _canon(containment["WORKROOT"])
    tmpdir = _canon(containment["TMPDIR"])
    config_dir = _canon(containment["CONFIG"])
    home = _canon(containment["HOME"])
    read_deny_root = containment.get("READ_DENY_ROOT") or ""
    extra_read_denies = list(containment.get("extra_read_denies") or [])
    extra_write_roots = list(containment.get("extra_write_roots") or [])
    read_policy = containment.get("read_policy")

    lines: list[str] = []
    lines.append("(version 1)")
    lines.append("(allow default)                          ; network + system-lib + /etc reads open by default")
    lines.append("")

    if read_policy is not None:
        _render_blinders_read_policy(lines, read_policy)
        control_sockets = list(read_policy.get("runtime_control_sockets") or [])
        if control_sockets:
            lines.append(";; --- RUNTIME CONTROL CONDUIT (exact daemon unix socket) ---")
            for control_socket in control_sockets:
                socket_path = os.path.normpath(os.path.abspath(control_socket))
                lines.append(
                    "(allow network-outbound "
                    f"(remote unix-socket (path-literal {json.dumps(socket_path)})))"
                )
            lines.append("")

    # --- WRITE JAIL (verified strong; deny-all-then-allow-list) ---
    lines.append(";; --- WRITE JAIL (verified strong; deny-all-then-allow-list) ---")
    lines.append("(deny file-write*)")
    lines.append("(allow file-write*")
    lines.append(f'  (subpath "{workroot}")                 ; node workspace, realpath-canonicalized')
    lines.append(f'  (subpath "{tmpdir}")                   ; per-session CLAUDE_CODE_TMPDIR')
    lines.append(f'  (subpath "{config_dir}")               ; CLAUDE_CONFIG_DIR — CC lock/state writes')
    if containment.get("allow_claude_home_write", True):
        lines.append(f'  (subpath "{_canon(os.path.join(home, ".claude"))}")  ; CC session logs/history (§8.1 gate 1)')
    # Additive extra write roots (§2.5a write_roots — additive only), each canonicalized (§2.4).
    for extra in extra_write_roots:
        lines.append(f'  (subpath "{_canon(extra)}")')
    lines.append('  (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr")')
    # /dev/ptmx is the pty MASTER multiplexer — opening it is how any pty gets allocated. The slave
    # side (/dev/ttysNNN) was already open via the regex below, so pty-backed execution was intended
    # all along; the master node was simply missed. Without it `openpty` returns EPERM and a runtime
    # that runs its commands on a pty (Codex 0.144's exec path) cannot execute ANYTHING, which also
    # means it cannot write its own FAILED sign-off. Not a read/exfiltration surface: a pty pair is
    # process-local and every file the seat reaches through it is still bound by the rules above.
    lines.append('  (literal "/dev/ptmx")')
    lines.append('  (regex #"^/dev/tty"))')
    lines.append(";; Dep-cache writes are kept INSIDE WORKROOT by env redirection (cache_redirect_env).")
    lines.append("")

    # --- KEYCHAIN: mach-service deny (the file-read deny does NOT protect it — securityd is mach) ---
    lines.append(";; --- KEYCHAIN: mach-service deny (securityd is a mach service, not file IO) ---")
    lines.append("(deny mach-lookup")
    lines.append('  (global-name "com.apple.SecurityServer")')
    lines.append('  (global-name "com.apple.securityd"))')
    lines.append("")

    # --- READ DENY: secrets (broad named set) ---
    lines.append(";; --- READ DENY: secrets (broad named set — the credential stores the review found readable) ---")
    lines.append("(deny file-read*")
    for rel in _SECRET_SUBPATH_RELS:
        lines.append(f'  (subpath "{home}/{rel}")')
    for rel in _SECRET_LITERAL_RELS:
        lines.append(f'  (literal "{home}/{rel}")')
    # The relocated/denied OAuth token literal (§3 — single-file deny even though config/ is writable).
    lines.append(f'  (literal "{config_dir}/.oauth_token")')
    # <EXTRA_READ_DENIES> — additional secret paths, each realpath-canonicalized (§2.4).
    for deny in extra_read_denies:
        lines.append(f'  (subpath "{_canon(deny)}")')
    lines.append("  )")
    lines.append("")

    # --- READ DENY: cross-project source (the WORKSPACE-SCHEMA read graph for L2–L5) ---
    if read_deny_root:
        lines.append(";; --- READ DENY: cross-project source (cousins / other projects, per address) ---")
        lines.append(f'(deny file-read* (subpath "{_canon(read_deny_root)}"))')
        lines.append("")

    # --- READ DENY: secret-pattern files anywhere (the sibling-.env guarantee, §1.3) ---
    lines.append(";; --- READ DENY: secret-pattern files anywhere (the sibling-.env guarantee, §1.3) ---")
    lines.append("(deny file-read*")
    lines.append(r'  (regex #"/\.env($|\.)")               ; **/.env, **/.env.*')
    lines.append(r'  (regex #"/(credentials|secrets)[^/]*$")')
    lines.append(r'  (regex #"\.pem$"))')
    # ...then re-allow the agent's OWN workspace secret-pattern files (last-match-wins). This MUST be
    # the LAST read rule so it scopes the secret-pattern deny to "outside WORKROOT" WITHOUT
    # un-denying siblings' .env (§2.3).
    lines.append(f'(allow file-read* (subpath "{workroot}"))')
    # CC MUST be able to READ its own CLAUDE_CONFIG_DIR (state/lock/settings). With (allow default) this
    # is already open when CONFIG is OUTSIDE READ_DENY_ROOT (the real harness: .cc-pinned/config). But a
    # CONFIG placed UNDER READ_DENY_ROOT (= the runtime root) would otherwise be read-DENIED by the
    # cross-project deny -> CC fails to boot. Re-allow CONFIG reads here (BEFORE the final token re-deny,
    # so the token under CONFIG stays closed). Defensive — makes CONFIG-anywhere boot correctly.
    lines.append(f'(allow file-read* (subpath "{config_dir}"))')
    lines.append("")

    # The runtime needs its exact mutable state root, but the shared Claude config contains other
    # seats' transcripts/tasks.  Deny-after-allow is a verified clean inner carve-out.
    if read_policy is not None:
        inner_denies = list(read_policy.get("runtime_inner_denies") or [])
        if inner_denies:
            lines.append(";; --- RUNTIME STATE INNER DENIES (cross-seat private surfaces) ---")
            lines.append("(deny file-read*")
            for deny in inner_denies:
                lines.append(f'  (subpath "{_canon(deny)}")')
            lines.append("  )")
            lines.append("")
        inner_allows = list(read_policy.get("runtime_inner_allows") or [])
        if inner_allows:
            lines.append(";; --- RUNTIME STATE EXACT RE-ALLOWS (this seat only) ---")
            lines.append("(allow file-read*")
            for allow in inner_allows:
                lines.append(f'  (subpath "{_canon(allow)}")')
            lines.append("  )")
            lines.append("")

    # --- FINAL: the OAuth token is NEVER agent-readable/writable, even if CONFIG sits under WORKROOT ---
    # The WORKROOT re-allow above is last-match-wins; if CONFIG is inside WORKROOT it would otherwise
    # UN-DENY the token literal (a latent read hole — blocker). Re-deny the token LAST (read AND write,
    # §3.2: a writable CONFIG must not let the agent rewrite the token either) so it is unconditionally
    # closed regardless of CONFIG's position. In the harness CONFIG (.cc-pinned/config) is NOT under a
    # node WORKROOT, so this is defense-in-depth — but it must hold even if that ever changes.
    lines.append(";; --- FINAL: OAuth token never agent-readable/writable (last-match-wins re-deny) ---")
    lines.append(f'(deny file-read* (literal "{config_dir}/.oauth_token"))')
    lines.append(f'(deny file-write* (literal "{config_dir}/.oauth_token"))')
    lines.append("")

    return "\n".join(lines)


@dataclass(frozen=True)
class ProfileProbe:
    ok: bool
    returncode: int
    stderr: str = ""


class ProfileApplicationError(RuntimeError):
    """An enforce-mode profile could not be rendered, compiled, or applied."""


@dataclass(frozen=True)
class PreparedLaunch:
    argv: list[str]
    posture: dict
    applied: bool


def probe_profile(profile_text: str) -> ProfileProbe:
    """Compile and apply ``profile_text`` to a no-op before the actor opens."""
    if not Path(SANDBOX_EXEC).is_file():
        return ProfileProbe(False, 127, f"sandbox-exec missing at {SANDBOX_EXEC}")
    try:
        result = subprocess.run(
            [SANDBOX_EXEC, "-p", profile_text, "/usr/bin/true"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProfileProbe(False, 126, str(exc))
    return ProfileProbe(result.returncode == 0, result.returncode, result.stderr.strip())


def profile_posture(
    containment: dict,
    profile_text: str,
    *,
    degraded_reason: str | None = None,
) -> dict:
    """Return the deterministic, secret-free binding posture for a rendered profile."""
    policy = containment.get("read_policy") or {}
    return {
        "version": policy.get("version", "legacy-containment"),
        "mode": policy.get("mode", "legacy"),
        "profile_sha256": hashlib.sha256(profile_text.encode("utf-8")).hexdigest(),
        "l1_god_view": bool(policy.get("l1_god_view", False)),
        "allow_literals": sorted(policy.get("allow_literals") or []),
        "allow_subpaths": sorted(policy.get("allow_subpaths") or []),
        "direct_surfaces": sorted(policy.get("direct_surfaces") or []),
        "runtime_state_roots": sorted(policy.get("runtime_state_roots") or []),
        "runtime_inner_denies": sorted(policy.get("runtime_inner_denies") or []),
        "runtime_inner_allows": sorted(policy.get("runtime_inner_allows") or []),
        "runtime_control_sockets": sorted(policy.get("runtime_control_sockets") or []),
        "degraded": bool(degraded_reason),
        **({"degraded_reason": degraded_reason} if degraded_reason else {}),
    }


def write_profile(
    profile_text: str,
    *,
    base_dir: str,
    session_name: str,
) -> str:
    """Write one stable per-session profile under a runtime-writable state root."""
    prof_dir = Path(base_dir) / ".sandbox-profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    safe = session_name.replace("/", "-").replace(":", "-")
    path = prof_dir / f"{safe}.sb"
    path.write_text(profile_text, encoding="utf-8")
    return str(path)


def prepare_launch(
    pane_argv: list[str],
    containment: dict,
    *,
    base_dir: str,
    session_name: str,
) -> PreparedLaunch:
    """Render, preflight, persist, and wrap one pane through a single runtime-neutral seam."""
    policy = containment.get("read_policy") or {}
    mode = policy.get("mode", "legacy")
    try:
        profile_text = render_profile(containment)
        probe = probe_profile(profile_text) if containment.get("read_policy") is not None else None
        if probe is not None and not probe.ok:
            raise ProfileApplicationError(
                f"sandbox profile apply failed rc={probe.returncode}: "
                f"{probe.stderr or 'no stderr'}"
            )
        profile_path = write_profile(
            profile_text,
            base_dir=base_dir,
            session_name=session_name,
        )
        posture = profile_posture(containment, profile_text)
        return PreparedLaunch(
            argv=wrap(pane_argv, profile_path),
            posture=posture,
            applied=True,
        )
    except Exception as exc:  # noqa: BLE001 - observe degradation must cover the whole apply seam.
        if mode == "enforce":
            if isinstance(exc, ProfileApplicationError):
                raise
            raise ProfileApplicationError(
                f"sandbox profile apply failed: {type(exc).__name__}: {exc}"
            ) from exc
        # Legacy containment predates observe and remains fail-closed on apply errors.
        if mode != "observe":
            raise
        profile_text = locals().get("profile_text", "")
        reason = f"sandbox profile degraded: {type(exc).__name__}: {exc}"
        return PreparedLaunch(
            argv=list(pane_argv),
            posture=profile_posture(
                containment,
                profile_text,
                degraded_reason=reason,
            ),
            applied=False,
        )


# ---------------------------------------------------------------------------
# wrap — the sandbox-exec invocation that wraps the env-i pane command (§7.1).
# ---------------------------------------------------------------------------

def wrap(pane_argv: list[str], profile_path: str) -> list[str]:
    """Build ``sandbox-exec -f <profile-file> <pane…>`` — the vector the tmux pane actually runs.

    The seatbelt prefix is the OUTSIDE of the detached pane's launch command-line (§7.1):
    ``sandbox-exec -f <profile>.sb env -i <K=V…> <binary> <flags>``. The from-empty ``env -i``
    clean-slate isolator stays the pane HEAD verbatim (sandbox-exec wraps it, does NOT replace it),
    so the Increment-9 OAuth-only isolation invariant still holds inside the jail. The pane vector
    is appended as a CONTIGUOUS, UNCHANGED tail — no re-quote round-trip that could re-expand a
    value (the tmux ``new-session`` argv-token contract).
    """
    return [SANDBOX_EXEC, "-f", str(profile_path), *list(pane_argv)]
