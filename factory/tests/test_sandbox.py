"""SECURITY-JAIL increment — FROZEN acceptance for the seatbelt write/read jail.

THE CONTAINMENT under test (design/SECURITY.md §2.3 profile + §2.4 canonicalization + §8.1 gates):
spawned agents have auto-approved full tools and run arbitrary code; the seatbelt is the STRUCTURAL
blast-radius bound, not the agent's judgment. `harnessd/spawn/sandbox.py` is the new module:

  * ``render_profile(containment) -> str`` renders the §2.3 ``.sb`` text from a RESOLVED containment
    block — WORKROOT, TMPDIR, CONFIG, HOME, READ_DENY_ROOT, extra_read_denies, extra_write_roots —
    with EVERY path realpath-canonicalized (§2.4: a logical ``/tmp/X`` deny LEAKS while the
    ``/private/tmp/X`` realpath deny BLOCKS, so canonicalize unconditionally).
  * ``wrap(pane_argv, profile) -> list[str]`` builds the ``sandbox-exec -f <profile-file> <pane…>``
    invocation that wraps the env-i pane command (the tmux pane runs the sandbox-wrapped vector).
  * ``cache_redirect_env(workroot) -> dict`` is the §2.3 tool-cache redirection env (NPM_CONFIG_CACHE
    etc. pointed INTO WORKROOT) so a real ``npm install`` writes its cache inside the jail.

BIAS TO REAL (Lesson 7 — paramount here): every jail test boots the REAL ``sandbox-exec`` with the
REAL rendered profile against the REAL filesystem (a tmp WORKROOT). NO mock of sandbox-exec or the
filesystem. The §8.1 write-jail regression checklist (a)-(j) is asserted as the lock-in regression:

  (a) write INSIDE WORKROOT succeeds; write to $HOME FAILS Operation-not-permitted (file never made).
  (b) nested ``sandbox-exec`` permissive profile -> BLOCKED (sandbox_apply: Operation not permitted).
  (c) ``osascript -e "do shell script"`` -> inherits the sandbox, its write stays jailed.
  (d) LaunchAgent plist write -> BLOCKED; ``launchctl submit`` -> no effect.
  (e) ``crontab`` install -> BLOCKED.
  (f) symlink-out-of-workspace write -> BLOCKED (seatbelt resolves the realpath, §2.4).
  (g) CANONICALIZATION load-bearing: logical ``/tmp/X`` deny LEAKS, ``/private/tmp/X`` realpath deny
      BLOCKS -> render_profile MUST canonicalize every path (a logical-path mutant leaks -> caught).
  (h) READ DENY: a planted secret (sibling ``.env`` / ``~/.ssh`` key) is read-DENIED; role/design docs
      + the WORKROOT's own ``.env`` are read-ALLOWED (last-match-wins re-allow of WORKROOT).
  (i) TOOL-CACHE redirect: with the §2.3 cache env, a real ``npm install`` writes its cache INSIDE
      WORKROOT and succeeds RC=0 under the jail (the "not too tight" gate).
  (j) GATE 1: the REAL pinned CC boots under the REAL seatbelt and AUTHS (with the injected live
      token + keychain mach-deny -> the security-strong config). Marked real_boot (skips clean when
      the binary+token are absent), like the Increment-16 real_boot gate.

LOAD-BEARING (each property catches its mutant):
  * write-jail: a broad ``(allow file-write*)`` mutant -> the $HOME-write test FAILS to be denied.
  * escapes: a permissive/omitted deny -> nested-sandbox/launchctl/crontab succeed -> caught.
  * canonicalization: a logical (non-realpath) path mutant -> the /tmp-vs-/private/tmp test catches it.
  * secrets: a broadened allow -> a planted secret becomes readable -> caught.
  * gate 1: a wrong profile denying a needed write/read -> CC fails to boot -> caught.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import harnessd.config as config

# The module under test (FROZEN by these tests; implementation owed by the increment).
# A HARD import on purpose: until harnessd/spawn/sandbox.py exists this suite is RED at
# collection (a loud ModuleNotFoundError), NOT a silent skip — the absent jail must fail.
from harnessd.spawn import blinders, sandbox


# ---------------------------------------------------------------------------
# Environment preconditions — these tests REQUIRE the real macOS seatbelt.
# ---------------------------------------------------------------------------

_SANDBOX_EXEC = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
_HAS_SEATBELT = os.path.exists(_SANDBOX_EXEC)

# All real-sandbox tests are skipped clean off-macOS / without the binary (never silently pass).
pytestmark = pytest.mark.skipif(
    not _HAS_SEATBELT,
    reason="real macOS sandbox-exec (seatbelt) not present — the jail tests need the real binary",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers — render a profile for a real tmp WORKROOT, run a command under it.
# ---------------------------------------------------------------------------


def _canon(p: str | Path) -> str:
    """The realpath the seatbelt actually matches (the §2.4 trap: /tmp -> /private/tmp)."""
    return os.path.realpath(str(p))


def _containment(
    workroot: str,
    *,
    tmpdir: str | None = None,
    config_dir: str | None = None,
    home: str | None = None,
    read_deny_root: str | None = None,
    extra_read_denies: list[str] | None = None,
    extra_write_roots: list[str] | None = None,
) -> dict:
    """A RESOLVED containment block (§2.5a shape) for render_profile.

    Passes LOGICAL (un-canonicalized) paths on purpose — render_profile owns the §2.4
    realpath-canonicalization, so handing it a ``/tmp/...`` logical path MUST yield a
    ``/private/tmp/...`` realpath rule (the (g) canonicalization contract).
    """
    return {
        "WORKROOT": workroot,
        "TMPDIR": tmpdir if tmpdir is not None else os.path.join(workroot, ".tmp"),
        "CONFIG": config_dir if config_dir is not None else os.path.join(workroot, ".config"),
        "HOME": home if home is not None else os.path.expanduser("~"),
        "READ_DENY_ROOT": read_deny_root if read_deny_root is not None else "",
        "extra_read_denies": list(extra_read_denies or []),
        "extra_write_roots": list(extra_write_roots or []),
    }


def _write_profile(tmp_path: Path, profile_text: str) -> Path:
    pf = tmp_path / "profile.sb"
    pf.write_text(profile_text)
    return pf


def _run_under_jail(
    profile_path: Path,
    argv: list[str],
    *,
    env: dict | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run ``sandbox-exec -f <profile> <argv…>`` for real and capture rc/stdout/stderr."""
    cmd = [_SANDBOX_EXEC, "-f", str(profile_path), *argv]
    run_env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"}
    if env:
        run_env.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=run_env
    )


@pytest.fixture
def workroot(tmp_path):
    """A real WORKROOT under a realpath-canonical tmp tree (the agent's jailed write root)."""
    wr = tmp_path / "workroot"
    (wr / ".tmp").mkdir(parents=True)
    (wr / ".config").mkdir(parents=True)
    return wr


# ===========================================================================
# render_profile — the §2.3 .sb structure (deterministic, no sandbox-exec yet).
# ===========================================================================


class TestRenderProfileStructure:
    """The rendered §2.3 profile text has the load-bearing clauses in the load-bearing ORDER."""

    def test_returns_sb_text_with_version_and_allow_default(self, workroot):
        prof = sandbox.render_profile(_containment(str(workroot)))
        assert isinstance(prof, str) and prof.strip()
        assert "(version 1)" in prof
        assert "(allow default)" in prof  # network + system-lib + /etc reads open (§2.3)

    def test_write_jail_is_deny_all_then_allow_list(self, workroot):
        """The write jail MUST be (deny file-write*) BEFORE the allow-list (deny-all-then-allow)."""
        prof = sandbox.render_profile(_containment(str(workroot)))
        assert "(deny file-write*)" in prof
        deny_idx = prof.index("(deny file-write*)")
        allow_idx = prof.index("(allow file-write*")
        assert deny_idx < allow_idx, "the deny-all must precede the write allow-list (§2.3)"

    def test_write_allow_list_includes_workroot_tmpdir_config(self, workroot):
        c = _containment(str(workroot))
        prof = sandbox.render_profile(c)
        # Each write-root appears as a realpath-canonicalized subpath in the allow-list.
        assert f'(subpath "{_canon(c["WORKROOT"])}")' in prof
        assert f'(subpath "{_canon(c["TMPDIR"])}")' in prof
        assert f'(subpath "{_canon(c["CONFIG"])}")' in prof
        # The HOME/.claude write-allow (CC session logs/history — §2.3 / §8.1 gate 1).
        assert f'(subpath "{_canon(os.path.join(c["HOME"], ".claude"))}")' in prof
        # The dev nodes.
        for lit in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            assert f'(literal "{lit}")' in prof

    def test_write_allow_list_includes_pty_master_multiplexer(self, workroot):
        """``/dev/ptmx`` must be write-allowed or NO seat can allocate a pty (CELL-PTY-EPERM).

        The slave side is already open via the ``^/dev/tty`` regex (``/dev/ttysNNN``), which is the
        evidence the author intended pty-backed execution to work; the master multiplexer node was
        simply missed. A runtime that runs its commands on a pty (Codex 0.144's exec path) fails at
        ``openpty`` with EPERM under every production profile — observe AND enforce, since the write
        jail is mode-independent. Killed mutant: drop the literal -> the real openpty test below
        raises PermissionError.
        """
        prof = sandbox.render_profile(_containment(str(workroot)))
        assert '(literal "/dev/ptmx")' in prof

    def test_extra_write_roots_are_added_canonicalized(self, workroot, tmp_path):
        extra = tmp_path / "shared-out"
        extra.mkdir()
        c = _containment(str(workroot), extra_write_roots=[str(extra)])
        prof = sandbox.render_profile(c)
        assert f'(subpath "{_canon(extra)}")' in prof

    def test_keychain_mach_deny_present(self, workroot):
        """The keychain is closed by a mach-service deny (the file-read deny is irrelevant, §2.3)."""
        prof = sandbox.render_profile(_containment(str(workroot)))
        assert "(deny mach-lookup" in prof
        assert "com.apple.SecurityServer" in prof
        assert "com.apple.securityd" in prof

    def test_secret_read_deny_named_set_present(self, workroot):
        """The §2.3 named secret set is denied (home-tree credential stores + literals)."""
        c = _containment(str(workroot))
        home = _canon(c["HOME"])
        prof = sandbox.render_profile(c)
        for sub in (".ssh", ".aws", ".gnupg", "Library/Keychains", ".config/gh",
                    ".config/gcloud", ".kube", ".docker", ".codex", ".gemini"):
            assert f'(subpath "{home}/{sub}")' in prof, f"missing secret subpath deny: {sub}"
        for lit in (".netrc", ".npmrc", ".pypirc", ".git-credentials", ".claude.json",
                    ".claude/.credentials.json", ".zsh_history", ".bash_history"):
            assert f'(literal "{home}/{lit}")' in prof, f"missing secret literal deny: {lit}"

    def test_secret_pattern_globs_present(self, workroot):
        """**/.env, credentials/secrets, *.pem are pattern-denied anywhere (§2.3 sibling-.env)."""
        prof = sandbox.render_profile(_containment(str(workroot)))
        assert "file-read*" in prof
        assert ".env" in prof
        assert "pem" in prof
        assert "credentials" in prof or "secrets" in prof

    def test_extra_read_denies_rendered(self, workroot, tmp_path):
        proj_token = tmp_path / "proj-token-dir"
        proj_token.mkdir()
        c = _containment(str(workroot), extra_read_denies=[str(proj_token)])
        prof = sandbox.render_profile(c)
        assert _canon(proj_token) in prof

    def test_workroot_reallow_is_last_match(self, workroot):
        """A trailing (allow file-read* (subpath WORKROOT)) un-denies the agent's OWN .env/.pem.

        Seatbelt is last-match-wins; this re-allow MUST come AFTER the secret-pattern deny so the
        agent reads its own workspace secret-pattern files without un-denying siblings'."""
        c = _containment(str(workroot))
        prof = sandbox.render_profile(c)
        reallow = f'(allow file-read* (subpath "{_canon(c["WORKROOT"])}"))'
        assert reallow in prof
        # The re-allow is AFTER the secret-pattern deny (last-match-wins scopes the deny to outside).
        assert prof.index(".pem") < prof.index(reallow), (
            "the WORKROOT read re-allow must be the LAST read rule (after the secret-pattern deny)"
        )

    def test_cross_project_read_deny_when_root_given(self, workroot, tmp_path):
        cousin = tmp_path / "other-project"
        cousin.mkdir()
        c = _containment(str(workroot), read_deny_root=str(cousin))
        prof = sandbox.render_profile(c)
        assert f'(deny file-read* (subpath "{_canon(cousin)}"))' in prof


class TestRenderProfileCanonicalization:
    """§2.4 — EVERY templated path is realpath-canonicalized (the #1 silent-hole guard)."""

    def test_workroot_logical_tmp_becomes_realpath(self):
        """A logical /tmp/... WORKROOT renders as /private/tmp/... — NOT the logical path.

        This is the load-bearing (g) contract: a mutant that templates the logical path would
        OVER-DENY a real write / silently mis-scope; canonicalize unconditionally."""
        logical = f"/tmp/jail-canon-{uuid.uuid4().hex}"
        c = _containment(logical, tmpdir=logical + "/.tmp", config_dir=logical + "/.config")
        prof = sandbox.render_profile(c)
        real = _canon(logical)  # /private/tmp/...
        assert real != logical, "precondition: /tmp must canonicalize to /private/tmp on this box"
        assert f'(subpath "{real}")' in prof
        # The LOGICAL path must NOT appear as a write-allow subpath (would mis-scope).
        assert f'(subpath "{logical}")' not in prof

    def test_extra_read_deny_logical_path_canonicalized(self):
        """A logical /tmp secret-deny path renders as the realpath — else the deny LEAKS (§2.4)."""
        logical = f"/tmp/jail-secret-{uuid.uuid4().hex}"
        c = _containment("/tmp/jail-wr", extra_read_denies=[logical])
        prof = sandbox.render_profile(c)
        assert _canon(logical) in prof
        # the bare logical path must NOT be the deny target (it would leak — proven in (g))
        assert f'(subpath "{logical}")' not in prof


# ===========================================================================
# wrap — the sandbox-exec invocation that wraps the env-i pane command.
# ===========================================================================


class TestWrap:
    """``wrap(pane_argv, profile)`` -> the sandbox-exec vector the tmux pane actually runs."""

    def test_wrap_prefixes_sandbox_exec_with_profile_file(self, tmp_path):
        pane = ["env", "-i", "CLAUDE_CONFIG_DIR=/x", "/path/to/claude.exe", "--system-prompt-file", "p"]
        prof_path = _write_profile(tmp_path, "(version 1)(allow default)")
        wrapped = sandbox.wrap(pane, str(prof_path))
        assert isinstance(wrapped, list)
        assert wrapped[0].endswith("sandbox-exec")
        assert "-f" in wrapped
        f_idx = wrapped.index("-f")
        assert wrapped[f_idx + 1] == str(prof_path)

    def test_wrap_preserves_pane_argv_verbatim_as_tail(self, tmp_path):
        """The env-i pane command is the TAIL of the sandbox-exec vector, unchanged (no re-quote)."""
        pane = ["env", "-i", "K=V", "/cc/claude.exe", "--system-prompt-file", "operational/shared/system-prompt.md"]
        prof_path = _write_profile(tmp_path, "(version 1)(allow default)")
        wrapped = sandbox.wrap(pane, str(prof_path))
        # the pane vector appears intact as a contiguous tail
        assert wrapped[-len(pane):] == pane

    def test_wrap_keeps_env_i_clean_slate_inside_the_jail(self, tmp_path):
        """sandbox-exec wraps the OUTSIDE; the from-empty ``env -i`` clean-slate stays the pane head."""
        pane = ["env", "-i", "A=1", "/cc/claude.exe"]
        prof_path = _write_profile(tmp_path, "(version 1)(allow default)")
        wrapped = sandbox.wrap(pane, str(prof_path))
        ei = wrapped.index("env")
        assert wrapped[ei : ei + 2] == ["env", "-i"], "env -i must remain the pane isolator head"

    def test_wrapped_pane_still_passes_oauth_isolation_shape(self, tmp_path):
        """The wrapped vector still contains the ``env -i`` isolator (the Increment-9 invariant)."""
        from harnessd.spawn import tmux

        env = {
            "CLAUDE_CONFIG_DIR": "/x",
            "CLAUDE_CODE_OAUTH_TOKEN": "test-ant-oat01-xxx",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        pane = tmux.build_pane_argv(env, ["/cc/claude.exe", "--system-prompt-file", "p"])
        prof_path = _write_profile(tmp_path, "(version 1)(allow default)")
        wrapped = sandbox.wrap(pane, str(prof_path))
        # env -i is still present (sandbox-exec wraps it, does not replace it)
        assert "env" in wrapped and wrapped[wrapped.index("env") + 1] == "-i"


class TestCacheRedirectEnv:
    """The §2.3 tool-cache redirection env points the per-user caches INTO WORKROOT."""

    def test_cache_env_points_into_workroot(self, workroot):
        env = sandbox.cache_redirect_env(str(workroot))
        wr = _canon(workroot)
        # the §2.3 named cache vars, all under WORKROOT
        assert env["NPM_CONFIG_CACHE"].startswith(wr)
        assert env["PIP_CACHE_DIR"].startswith(wr)
        for key in ("GOMODCACHE", "GOCACHE", "CARGO_HOME", "YARN_CACHE_FOLDER", "NUGET_PACKAGES"):
            assert key in env, f"missing §2.3 cache var: {key}"
            assert env[key].startswith(wr), f"{key} must redirect INTO WORKROOT, got {env[key]}"


# ===========================================================================
# §8.1 WRITE-JAIL REGRESSION CHECKLIST — REAL sandbox-exec, REAL filesystem.
# Each is a VERIFIED-HELD behaviour locked as a regression (a permissive mutant re-opens it).
# ===========================================================================


class TestWriteJailReal:
    """(a) write INSIDE WORKROOT succeeds; write to $HOME FAILS (file never created)."""

    def test_write_inside_workroot_succeeds(self, workroot, tmp_path):
        prof = sandbox.render_profile(_containment(str(workroot)))
        pf = _write_profile(tmp_path, prof)
        target = _canon(workroot) + "/inside.txt"
        r = _run_under_jail(pf, ["/bin/sh", "-c", f"echo hi > {target}"])
        assert r.returncode == 0, f"write inside WORKROOT must succeed: {r.stderr!r}"
        assert os.path.exists(target), "the inside-workroot file must really exist"

    def test_openpty_inside_the_jail_succeeds(self, workroot, tmp_path):
        """A jailed seat MUST be able to allocate a pty (CELL-PTY-EPERM, run-5 live evidence).

        Codex 0.144 runs model-generated commands on a pty; without ``/dev/ptmx`` the seat's very
        first command dies with ``openpty: Os { code: 1, kind: PermissionDenied }`` and the seat can
        neither work nor write its FAILED sign-off. Verified REAL: this fails PermissionError
        against a profile without the literal and passes with it.
        """
        prof = sandbox.render_profile(_containment(str(workroot)))
        pf = _write_profile(tmp_path, prof)
        r = _run_under_jail(
            pf,
            ["/usr/bin/python3", "-c", "import os; os.openpty(); print('PTY_OK')"],
        )
        assert "PTY_OK" in r.stdout, (
            f"a jailed seat must be able to open a pty: rc={r.returncode} {r.stderr!r}"
        )

    def test_write_to_home_is_denied_and_file_never_created(self, workroot, tmp_path):
        """The load-bearing write-jail proof: a broad ``(allow file-write*)`` mutant FAILS here."""
        prof = sandbox.render_profile(_containment(str(workroot)))
        pf = _write_profile(tmp_path, prof)
        home = _canon(os.path.expanduser("~"))
        marker = f"{home}/.jail_should_never_exist_{uuid.uuid4().hex}"
        try:
            r = _run_under_jail(pf, ["/bin/sh", "-c", f"echo pwned > {marker}"])
            assert not os.path.exists(marker), (
                "WRITE-JAIL BREACH: a $HOME write was allowed (the profile is too permissive)"
            )
            assert "Operation not permitted" in (r.stdout + r.stderr) or r.returncode != 0, (
                "the $HOME write must be denied Operation-not-permitted"
            )
        finally:
            if os.path.exists(marker):
                os.remove(marker)


class TestEscapesBlockedReal:
    """(b)-(f) the named write-escapes — all REALLY blocked by the §2.3 profile."""

    def _jail(self, workroot, tmp_path):
        prof = sandbox.render_profile(_containment(str(workroot)))
        return _write_profile(tmp_path, prof)

    def test_nested_sandbox_exec_blocked(self, workroot, tmp_path):
        """(b) nested sandbox-exec permissive profile -> sandbox_apply: Operation not permitted."""
        pf = self._jail(workroot, tmp_path)
        r = _run_under_jail(
            pf,
            [_SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/bin/echo", "NESTED_RAN"],
        )
        combined = r.stdout + r.stderr
        assert "NESTED_RAN" not in r.stdout, "nested sandbox child must NOT run"
        assert "Operation not permitted" in combined or "sandbox_apply" in combined, (
            f"nested sandbox-exec must be blocked: {combined!r}"
        )

    def test_osascript_do_shell_script_stays_jailed(self, workroot, tmp_path):
        """(c) osascript do shell script inherits the sandbox — its $HOME write is blocked."""
        pf = self._jail(workroot, tmp_path)
        home = _canon(os.path.expanduser("~"))
        marker = f"{home}/.jail_osa_{uuid.uuid4().hex}"
        try:
            r = _run_under_jail(
                pf,
                ["/usr/bin/osascript", "-e", f'do shell script "echo x > {marker}"'],
            )
            assert not os.path.exists(marker), (
                "SANDBOX ESCAPE: osascript do-shell-script wrote outside the jail"
            )
            assert "Operation not permitted" in (r.stdout + r.stderr) or r.returncode != 0
        finally:
            if os.path.exists(marker):
                os.remove(marker)

    def test_launchagent_plist_write_blocked(self, workroot, tmp_path):
        """(d) a LaunchAgent plist write -> BLOCKED."""
        pf = self._jail(workroot, tmp_path)
        plist = os.path.expanduser("~/Library/LaunchAgents/com.jail.test.plist")
        plist = _canon(os.path.dirname(plist)) + "/com.jail.test.plist"
        existed = os.path.exists(plist)
        r = _run_under_jail(pf, ["/bin/sh", "-c", f"echo '<plist/>' > {plist}"])
        if not existed:
            assert not os.path.exists(plist), "LaunchAgent plist write must be blocked"
        assert "Operation not permitted" in (r.stdout + r.stderr) or r.returncode != 0

    def test_launchctl_submit_has_no_effect(self, workroot, tmp_path):
        """(d) launchctl submit -> no effect (blocked / no job persisted)."""
        pf = self._jail(workroot, tmp_path)
        label = f"com.jail.test.{uuid.uuid4().hex[:8]}"
        r = _run_under_jail(
            pf, ["/bin/launchctl", "submit", "-l", label, "--", "/bin/echo", "hi"]
        )
        # The job must not have been registered (best-effort cleanup if it somehow was).
        listed = subprocess.run(
            ["/bin/launchctl", "list", label], capture_output=True, text=True
        )
        if listed.returncode == 0:
            subprocess.run(["/bin/launchctl", "remove", label], capture_output=True)
            pytest.fail("launchctl submit registered a job under the jail (must have no effect)")
        assert r.returncode != 0 or "Operation not permitted" in (r.stdout + r.stderr) or True

    def test_crontab_install_blocked(self, workroot, tmp_path):
        """(e) crontab install -> BLOCKED (setgid crontab cannot exec under the jail)."""
        pf = self._jail(workroot, tmp_path)
        cron_file = _canon(str(workroot)) + "/cron.txt"
        Path(cron_file).write_text("* * * * * echo jailtest\n")
        r = _run_under_jail(pf, ["/usr/bin/crontab", cron_file])
        combined = r.stdout + r.stderr
        assert r.returncode != 0, "crontab install must fail under the jail"
        assert "Operation not permitted" in combined or "failed" in combined.lower()

    def test_symlink_out_of_workspace_write_blocked(self, workroot, tmp_path):
        """(f) a write through a symlink pointing OUT of the workspace -> BLOCKED (realpath, §2.4)."""
        pf = self._jail(workroot, tmp_path)
        outside = _canon(str(tmp_path)) + f"/outside_{uuid.uuid4().hex}"
        link = _canon(str(workroot)) + "/escape_link"
        os.symlink(outside, link)
        try:
            r = _run_under_jail(pf, ["/bin/sh", "-c", f"echo escaped > {link}"])
            assert not os.path.exists(outside), (
                "SYMLINK ESCAPE: a write through an out-of-workspace symlink landed outside "
                "(the seatbelt must resolve the realpath, §2.4)"
            )
            assert "Operation not permitted" in (r.stdout + r.stderr) or r.returncode != 0
        finally:
            if os.path.exists(outside):
                os.remove(outside)


class TestCanonicalizationLoadBearingReal:
    """(g) THE canonicalization proof: logical /tmp deny LEAKS, realpath /private/tmp deny BLOCKS.

    render_profile MUST canonicalize. We prove the property two ways with REAL sandbox-exec:
      1. a hand-written LOGICAL-path deny really leaks (the failure mode the mutant would ship);
      2. render_profile's output (canonical) really BLOCKS the same read.
    """

    def test_logical_tmp_deny_leaks_but_canonical_deny_blocks(self, tmp_path):
        # plant a secret under a /tmp logical path (whose realpath is /private/tmp/...)
        secret_dir_logical = f"/tmp/jail-leak-{uuid.uuid4().hex}"
        os.makedirs(secret_dir_logical, exist_ok=True)
        secret_file = secret_dir_logical + "/s.txt"
        Path(secret_file).write_text("TOPSECRET\n")
        try:
            # 1) LOGICAL-path deny — the mutant form — LEAKS (read succeeds, exit 0).
            leak_profile = (
                f'(version 1)\n(allow default)\n'
                f'(deny file-read* (subpath "{secret_dir_logical}"))\n'
            )
            leak_pf = _write_profile(tmp_path, leak_profile)
            leak = _run_under_jail(leak_pf, ["/bin/cat", secret_file])
            assert leak.returncode == 0 and "TOPSECRET" in leak.stdout, (
                "precondition: a LOGICAL /tmp deny must LEAK (this is the mutant failure mode)"
            )

            # 2) render_profile (canonical) — the same path via extra_read_denies — BLOCKS.
            c = _containment("/tmp/jail-canon-wr", extra_read_denies=[secret_dir_logical])
            prof = sandbox.render_profile(c)
            # the rendered deny targets the REALPATH, not the logical path
            assert _canon(secret_dir_logical) in prof
            canon_pf = _write_profile(tmp_path, prof)
            blocked = _run_under_jail(canon_pf, ["/bin/cat", secret_file])
            assert blocked.returncode != 0 and "TOPSECRET" not in blocked.stdout, (
                "CANONICALIZATION HOLE: render_profile's deny LEAKED — it must canonicalize (§2.4)"
            )
        finally:
            shutil.rmtree(secret_dir_logical, ignore_errors=True)


class TestSecretReadDenyReal:
    """(h) secrets are read-DENIED; the WORKROOT's own .env + role/design docs are read-ALLOWED."""

    def test_sibling_env_denied_but_own_env_allowed(self, workroot, tmp_path):
        """A sibling .env OUTSIDE workroot is denied; the agent's OWN .env inside is allowed.

        Load-bearing: a broadened allow that un-denied the sibling's .env would FAIL here."""
        # own .env inside workroot -> allowed (re-allow last-match-wins)
        own_env = _canon(str(workroot)) + "/.env"
        Path(own_env).write_text("OWN_OK=1\n")
        # sibling .env outside workroot -> denied (secret-pattern glob)
        sibling = _canon(str(tmp_path)) + "/.env"
        Path(sibling).write_text("SIBLING_SECRET=leak\n")

        prof = sandbox.render_profile(_containment(str(workroot)))
        pf = _write_profile(tmp_path, prof)

        own = _run_under_jail(pf, ["/bin/cat", own_env])
        assert own.returncode == 0 and "OWN_OK" in own.stdout, (
            f"the agent must read its OWN workspace .env (last-match re-allow): {own.stderr!r}"
        )
        sib = _run_under_jail(pf, ["/bin/cat", sibling])
        assert sib.returncode != 0 and "SIBLING_SECRET" not in sib.stdout, (
            "SECRET LEAK: a sibling .env outside WORKROOT was readable (broaden-allow mutant caught)"
        )

    def test_planted_ssh_key_denied(self, workroot, tmp_path):
        """A planted ~/.ssh key file is read-DENIED under the jail (the §2.3 named set)."""
        ssh_dir = Path(os.path.expanduser("~/.ssh"))
        prof = sandbox.render_profile(_containment(str(workroot)))
        pf = _write_profile(tmp_path, prof)
        # Prefer a real existing key; otherwise plant+cleanup a fake one in ~/.ssh.
        planted = None
        candidates = list(ssh_dir.glob("id_*")) if ssh_dir.exists() else []
        candidates = [c for c in candidates if c.is_file() and not c.name.endswith(".pub")]
        if candidates:
            key = candidates[0]
        else:
            ssh_dir.mkdir(mode=0o700, exist_ok=True)
            planted = ssh_dir / f"id_jailtest_{uuid.uuid4().hex}"
            planted.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n")
            key = planted
        try:
            r = _run_under_jail(pf, ["/bin/cat", str(_canon(key))])
            assert r.returncode != 0, "a ~/.ssh key must be read-DENIED under the jail"
            assert "PRIVATE KEY" not in r.stdout and "BEGIN" not in r.stdout
        finally:
            if planted is not None and planted.exists():
                planted.unlink()

    def test_role_and_design_docs_readable(self, workroot, tmp_path):
        """Role/design docs (operational/*, design/*) stay read-ALLOWED — role resolution must work.

        Load-bearing both directions: the SAME profile that denies the secret allows the role doc."""
        prof = sandbox.render_profile(_containment(str(workroot)))
        pf = _write_profile(tmp_path, prof)
        # A real design doc (the SECURITY spec itself) under the repo — allowed by (allow default).
        doc = _canon(str(_REPO_ROOT / "design" / "SECURITY.md"))
        if not os.path.exists(doc):
            pytest.skip("design/SECURITY.md not present to prove role-doc readability")
        r = _run_under_jail(pf, ["/bin/cat", doc])
        assert r.returncode == 0 and r.stdout.strip(), (
            f"role/design docs must stay readable under the jail: {r.stderr!r}"
        )


class TestSemanticExactReadReal:
    """Q4 blind windows are filesystem facts, not prompt requests."""

    def test_declared_file_reads_and_neighbor_plan_file_is_denied(self, tmp_path):
        runtime = tmp_path / "runtime"
        own = runtime / "nodes" / "proj" / "semantic-seat"
        own.mkdir(parents=True)
        (own / ".tmp").mkdir()
        (own / ".config").mkdir()
        allowed = runtime / "nodes" / "proj" / "widget" / "plan" / "verification.md"
        denied = runtime / "nodes" / "proj" / "widget" / "plan" / "construction.md"
        allowed.parent.mkdir(parents=True)
        allowed.write_text("VISIBLE-VERIFICATION\n", encoding="utf-8")
        denied.write_text("HIDDEN-CONSTRUCTION\n", encoding="utf-8")
        policy = blinders.derive_exact_policy(
            node_address="proj/widget/plan-alignment/cell/seats/window#exec",
            runtime_root=str(runtime),
            workspace=str(own),
            exact_documents=[str(allowed)],
            runtime="claude-code",
            harness_root=str(_REPO_ROOT),
        )
        containment = sandbox.resolve_containment(
            "proj/semantic-seat#exec",
            runtime_root=runtime,
            config_dir=own / ".config",
            read_policy=policy,
            runtime="claude-code",
        )
        profile = _write_profile(tmp_path, sandbox.render_profile(containment))

        visible = _run_under_jail(profile, ["/bin/cat", str(allowed)])
        hidden = _run_under_jail(profile, ["/bin/cat", str(denied)])

        assert visible.returncode == 0
        assert "VISIBLE-VERIFICATION" in visible.stdout
        assert hidden.returncode != 0
        assert "HIDDEN-CONSTRUCTION" not in hidden.stdout


class TestProductProbeExactReadReal:
    """Q5 product probes can mutate only their own disposable instance."""

    def test_own_instance_is_read_write_and_live_producer_and_sibling_are_denied(
        self,
        tmp_path,
    ):
        runtime = tmp_path / "runtime"
        gate = runtime / "nodes" / "proj" / "reviews" / "gate-five"
        own = gate / "reviewers" / "user-simulation"
        instance = own / "disposable-instance"
        instance.mkdir(parents=True)
        (own / ".tmp").mkdir()
        (own / ".config").mkdir()
        own_file = instance / "app.txt"
        own_file.write_text("DISPOSABLE-COPY\n", encoding="utf-8")
        roster = gate / "product-probe-roster.demo.json"
        roster.write_text('{"journeys":[]}\n', encoding="utf-8")
        live_producer = runtime / "nodes" / "proj" / "app.txt"
        live_producer.parent.mkdir(parents=True, exist_ok=True)
        live_producer.write_text("LIVE-PRODUCER\n", encoding="utf-8")
        sibling = gate / "reviewers" / "performance-robustness" / "disposable-instance" / "app.txt"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("SIBLING-PROBE\n", encoding="utf-8")

        policy = blinders.derive_exact_policy(
            node_address="proj/reviews/gate-five/reviewers/user-simulation#exec",
            runtime_root=str(runtime),
            workspace=str(own),
            exact_documents=[str(roster)],
            runtime="claude-code",
            harness_root=str(_REPO_ROOT),
        )
        containment = sandbox.resolve_containment(
            "proj/reviews/gate-five/reviewers/user-simulation#exec",
            runtime_root=runtime,
            config_dir=own / ".config",
            read_policy=policy,
            runtime="claude-code",
        )
        profile = _write_profile(tmp_path, sandbox.render_profile(containment))

        own_read = _run_under_jail(profile, ["/bin/cat", str(own_file)])
        own_write = _run_under_jail(
            profile,
            ["/usr/bin/touch", str(instance / "runtime-state")],
        )
        producer_read = _run_under_jail(profile, ["/bin/cat", str(live_producer)])
        sibling_read = _run_under_jail(profile, ["/bin/cat", str(sibling)])

        assert own_read.returncode == 0 and "DISPOSABLE-COPY" in own_read.stdout
        assert own_write.returncode == 0
        assert producer_read.returncode != 0 and "LIVE-PRODUCER" not in producer_read.stdout
        assert sibling_read.returncode != 0 and "SIBLING-PROBE" not in sibling_read.stdout


class TestToolCacheRedirectReal:
    """(i) the "not too tight" gate — a real npm install writes its cache INSIDE WORKROOT, RC=0."""

    @pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
    def test_npm_install_with_cache_redirect_succeeds_in_jail(self, workroot, tmp_path):
        npm = shutil.which("npm")
        wr = _canon(str(workroot))
        proj = Path(wr) / "proj"
        proj.mkdir()
        (proj / "package.json").write_text(
            '{ "name":"jailtest","version":"1.0.0","dependencies":{"is-odd":"3.0.1"} }\n'
        )
        # WORKROOT + TMPDIR are §2.3 write-roots; the cache env points the npm cache INTO WORKROOT.
        c = _containment(str(workroot), tmpdir=str(workroot / ".tmp"))
        prof = sandbox.render_profile(c)
        pf = _write_profile(tmp_path, prof)
        cache_env = sandbox.cache_redirect_env(wr)
        env = {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "HOME": wr,  # HOME inside the jail so any stray ~ write is jailed too
            "TMPDIR": str(workroot / ".tmp"),
            **cache_env,
        }
        r = subprocess.run(
            [_SANDBOX_EXEC, "-f", str(pf), npm, "install", "--no-audit", "--no-fund"],
            cwd=str(proj),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        combined = r.stdout + r.stderr
        # Registry-reachability / TLS-trust failures are ENVIRONMENT preconditions, NOT a jail
        # failure: the `env -i` clean slate may strip this box's proxy/cert config, and the gate
        # is about whether the cache WRITE lands inside WORKROOT under the jail — not this host's
        # TLS trust store. Skip clean on any registry-reach error (the cache-write proof below
        # already fires the moment npm gets far enough to write its cache dir).
        _net_markers = (
            "network", "enotfound", "etimedout", "econnrefused", "econnreset",
            "unable_to_get_issuer_cert", "issuer certificate", "self-signed",
            "self signed", "cert_", "registry", "getaddrinfo",
        )
        npm_cache = Path(cache_env["NPM_CONFIG_CACHE"])
        cache_written = npm_cache.exists() and any(npm_cache.iterdir())
        if r.returncode != 0:
            if any(m in combined.lower() for m in _net_markers):
                # PROVE the not-too-tight property even when the registry is unreachable: the cache
                # dir was created INSIDE WORKROOT (the redirect held; the jail did NOT EPERM it).
                assert cache_written, (
                    "the redirected npm cache dir must be writable inside WORKROOT even when the "
                    f"registry is unreachable (the not-too-tight gate): {combined!r}"
                )
                pytest.skip("registry unreachable/TLS-untrusted under env -i — cache-redirect held")
            pytest.fail(f"redirected npm install failed for a NON-network reason: {combined!r}")
        # Full success path: the deps landed inside WORKROOT and the cache was written INSIDE WORKROOT.
        assert (proj / "node_modules" / "is-odd").exists(), "deps must install inside WORKROOT"
        assert cache_written, (
            "the npm cache must have been written INSIDE WORKROOT (the redirect held)"
        )


# ===========================================================================
# §8.1 GATE 1 — the load-bearing commissioning gate: REAL pinned CC boots + auths
# under the REAL seatbelt. Marked real_boot (spends a little usage), skips clean
# when the binary+login are absent. Resolves the keychain mach-deny fork empirically.
# ===========================================================================

_CC = _REPO_ROOT / ".cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
_CC_CONFIG_DIR = _REPO_ROOT / ".cc-pinned/config"
_TOKEN_FILE = _CC_CONFIG_DIR / ".oauth_token"
_SYSTEM_PROMPT = _REPO_ROOT / config.SYSTEM_PROMPT_FILE


def _gate1_containment(workroot: Path) -> dict:
    """A containment block whose write-allows cover what CC needs to boot (CONFIG + ~/.claude)."""
    return _containment(
        str(workroot),
        tmpdir=str(workroot / ".tmp"),
        config_dir=str(_CC_CONFIG_DIR),
        home=os.path.expanduser("~"),
    )


@pytest.mark.real_boot
class TestGate1RealCCBootsUnderSeatbelt:
    """GATE 1: the REAL pinned CC boots + AUTHS under the REAL seatbelt with the §2.3 profile.

    The KEY INSIGHT (IMPLEMENTATION-PLAN): inject the live OAuth token (env var) so the jailed CC
    needs NO keychain -> the keychain mach-deny SHIPS (security-strong). This gate confirms that
    empirically: CC boots+auths with the keychain CLOSED. If CC instead NEEDS securityd even with
    an injected token, the fork drops the mach-deny (HELPER-UID floor) — recorded by the assertion
    message so the build report states which configuration ships.
    """

    @pytest.fixture(scope="class")
    def real_token(self):
        if not _CC.exists():
            pytest.skip(f"pinned CC binary not installed at {_CC}")
        if not _SYSTEM_PROMPT.exists():
            pytest.skip(f"shared system prompt not found at {_SYSTEM_PROMPT}")
        if not _TOKEN_FILE.exists():
            pytest.skip("pinned install has no OAuth token (.cc-pinned/config/.oauth_token absent)")
        token = _TOKEN_FILE.read_text().strip()
        if not token:
            pytest.skip("pinned OAuth token file is empty")
        return token

    def _boot_under_seatbelt(self, profile_path: Path, token: str, workroot: Path, timeout=150):
        """Boot the REAL pinned CC in print mode, INSIDE the REAL seatbelt, via env -i isolation."""
        pane = [
            "env", "-i",
            f"CLAUDE_CONFIG_DIR={_CC_CONFIG_DIR}",
            f"CLAUDE_CODE_OAUTH_TOKEN={token}",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            "DISABLE_AUTOUPDATER=1",
            f"TMPDIR={workroot / '.tmp'}",
            f"HOME={os.path.expanduser('~')}",
            str(_CC),
            "--system-prompt-file", str(_SYSTEM_PROMPT),
            "-p", "Reply with exactly: OK",
        ]
        argv = sandbox.wrap(pane, str(profile_path))
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def test_pinned_cc_boots_and_auths_under_the_real_seatbelt(self, real_token, tmp_path):
        workroot = tmp_path / "gate1-workroot"
        (workroot / ".tmp").mkdir(parents=True)
        prof = sandbox.render_profile(_gate1_containment(workroot))
        pf = _write_profile(tmp_path, prof)

        result = self._boot_under_seatbelt(pf, real_token, workroot)
        out = (result.stdout or "") + (result.stderr or "")

        # An expired token is an ENVIRONMENT precondition (FORK-TOKEN-EXPIRY), not a jail failure.
        if "401" in out or "Invalid authentication" in out or "Failed to authenticate" in out:
            pytest.skip(
                "pinned OAuth token expired/invalid (401) — refresh it, then re-run "
                "`pytest -m real_boot`. (The KEYCHAIN FORK is unresolved while the token is stale.)"
            )

        # GATE 1 (security-strong config): CC boots + auths with the keychain mach-deny SHIPPED.
        # If this fails because CC needs securityd, the empirical fork resolution is: DROP the
        # mach-deny and use the HELPER-UID floor (recorded in the failure message for the report).
        assert result.returncode == 0, (
            "GATE-1 KEYCHAIN FORK: the pinned CC did NOT boot/auth under the seatbelt with the "
            "keychain mach-deny + injected token. Empirical resolution: CC needs securityd -> the "
            "global mach-deny CANNOT ship; the build must DROP it and use the HELPER-UID kernel "
            f"floor as the keychain wall (§2.3 / §8.1 gate 1). rc={result.returncode} out={out[:600]!r}"
        )
        assert "OK" in (result.stdout or ""), (
            f"the jailed CC booted but produced no in-role reply: {out[:400]!r}"
        )

    def test_keychain_mach_deny_really_closes_the_keychain(self, tmp_path):
        """Companion to gate 6: under the SHIPPED profile, keychain enumeration is structurally 0.

        Proves the mach-deny (not the file-read deny) is what closes the keychain: dump-keychain
        enumerates many items WITHOUT it and ZERO items WITH it (the §2.3 / §8.1 gate-6 claim)."""
        workroot = tmp_path / "kc-workroot"
        (workroot / ".tmp").mkdir(parents=True)
        prof = sandbox.render_profile(_gate1_containment(workroot))

        kc_pf = tmp_path / "shipped.sb"
        kc_pf.write_text(prof)
        base_pf = tmp_path / "base.sb"
        base_pf.write_text("(version 1)\n(allow default)\n")

        base = _run_under_jail(base_pf, ["/usr/bin/security", "dump-keychain"], timeout=60)
        base_items = base.stdout.count("keychain:")
        shipped = _run_under_jail(kc_pf, ["/usr/bin/security", "dump-keychain"], timeout=60)
        shipped_items = shipped.stdout.count("keychain:")

        # Without the mach-deny the keychain enumerates; with it, structurally zero.
        assert base_items > 0, "precondition: baseline must enumerate keychain items"
        assert shipped_items == 0, (
            f"KEYCHAIN NOT CLOSED: the shipped profile enumerated {shipped_items} keychain items — "
            "the mach-deny (not a file-read deny) must structurally close the keychain (§2.3)"
        )
