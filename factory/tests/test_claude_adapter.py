"""Increment 9 — FROZEN acceptance for the Claude-Code adapter (part (a): DRY-RUN argv/env).

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.11 — adapters/base.py (`RuntimeAdapter.pin_and_open`) +
    adapters/claude_code.py (the FROZEN boot recipe): argv = [CC, "--system-prompt-file",
    system_prompt_file] where system_prompt_file is the per-spawn composed identity bundle
    (`.identity-prompt.md`: shared prompt first, then the selected identity trio); env = exactly
    the 4 isolation vars; session name == "harness:"+collapse(address); record model_used /
    role_variant / system_prompt_file / system_prompt_file_hash / transcript_path.
  * IMPLEMENTATION-PLAN §6.2 dry-run done-test (L596-619): mock tmux + mock subprocess, assert NO real
    claude.exe exec; argv identical across role_variants; never --bare/--append-system-prompt/--agents.
  * DAEMON §6.2 (H40 recipe) + §6.3 (E32 spawn-failure contract; always record model_used; Codex =
    native Codex instructions plus neutral brief/boot prompt).
  * config.SYSTEM_PROMPT_FILE / PINNED_BINARY_VERSION (the config seats — NOT re-hardcoded).

This is part (a): a PURE-ASSEMBLY dry-run. It mocks the subprocess (justified: it asserts the
assembled argv/env WITHOUT executing — there is nothing real to run in a pure-assembly test, and NO
model may be called). The REAL-tmux tests live in test_tmux.py / test_mock_contract.py.

NO IMPLEMENTATION here — harnessd/spawn/adapters/* do not exist yet (RED first).

Load-bearing properties (each pins a mutant):
  * argv uses the COMPOSED identity bundle, NOT a bare per-level role path
        (mutant: per-level role.md -> caught).
  * argv is IDENTICAL across producer role_variants L1..L4 except for sanctioned policy flags
        (mutant: role text/path in argv -> caught).
  * argv NEVER carries --bare / --append-system-prompt / --agents / --agent (mutant: any -> caught).
  * env is EXACTLY the 4-var set (mutant: extra/missing var -> caught).
  * session name == "harness:" + collapse(address) (mutant: raw address / wrong prefix -> caught).
  * the role rides the brief/load-manifest, NOT the argv/prompt (mutant: role text in argv -> caught).
  * NO real subprocess exec of claude.exe (mutant: real Popen -> caught by the no-exec spy).
  * model_used is ALWAYS recorded == "opus-5.0 / claude-code" (mutant: drop model_used -> caught).
  * transcript_path is non-null and derived from session_uuid (mutant: null / unrelated -> caught).
  * Codex adapter pins GPT-5.5, preflights ChatGPT/OAuth auth, isolates per-worker CODEX_HOME state,
    and never permits OPENAI_API_KEY.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import pathlib
import subprocess
import tomllib

import pytest

from harnessd import addressing, config, turn_state
from harnessd.spawn import codex_auth, oauth_guard


def _base():
    return importlib.import_module("harnessd.spawn.adapters.base")


def _claude():
    return importlib.import_module("harnessd.spawn.adapters.claude_code")


def _codex():
    return importlib.import_module("harnessd.spawn.adapters.codex")


def _fake_jwt(claims):
    def enc(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.signature"


def _write_codex_auth(home, *, access_exp=2_000_000_000, id_exp=2_000_000_000):
    home.mkdir(parents=True, exist_ok=True)
    access = _fake_jwt(
        {
            "iss": "https://auth.example.test",
            "aud": ["https://api.example.test"],
            "iat": 1_000,
            "exp": access_exp,
            "session_id": "fake-session-id",
        }
    )
    id_token = _fake_jwt(
        {
            "iss": "https://auth.example.test",
            "aud": ["client"],
            "iat": 1_000,
            "exp": id_exp,
        }
    )
    refresh = "fake-canonical-refresh-token"
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": access,
                    "id_token": id_token,
                    "refresh_token": refresh,
                    "account_id": "fake-account-id",
                },
                "last_refresh": "2026-06-16T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return refresh


# The exact 4-var isolation set (DAEMON §6.2). OAuth token present (positive check passes),
# NO ANTHROPIC_API_KEY / OPENAI_API_KEY (negative invariant passes).
def _iso_env():
    return {
        "CLAUDE_CONFIG_DIR": "/HARNESS/.cc-pinned/config",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-xyz",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


_EXPECTED_ENV_KEYS = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
    }
)

_FORBIDDEN_FLAGS = ("--bare", "--append-system-prompt", "--agents", "--agent")


@pytest.fixture
def no_real_exec(monkeypatch):
    """Spy that asserts NO real subprocess EXEC happens during a dry-run assembly.

    The adapter's tmux dependency is mocked (see make_adapter); this is a second belt:
    if any code path reaches subprocess.Popen/run/call to actually launch claude.exe,
    fail the test. (Pure-assembly dry-run: there is nothing real to run; NO model burn.)
    """
    import subprocess

    calls = []

    def _boom(*a, **k):  # pragma: no cover - only fires on the mutant
        calls.append((a, k))
        raise AssertionError(
            "a DRY-RUN assembly test must NOT exec a real subprocess (claude.exe) — "
            f"saw subprocess call: args={a!r} kwargs={k!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "call", _boom)
    return calls


class _MockTmux:
    """Mock tmux for the dry-run: records create_detached args, returns a fake pane id,
    NEVER touches a real server (server_env clean). Provides the SAME from-empty
    `build_pane_argv` seam the real wrapper exposes so the adapter assembles the pane the
    same way it would in production — but NO real exec happens (no_real_exec spy proves it)."""

    def __init__(self):
        self.created = []

    def build_pane_argv(self, env, argv):
        pane = ["env", "-i"]
        for k, v in env.items():
            pane.append(f"{k}={v}")
        pane += list(argv)
        return pane

    def create_detached(self, session_name, pane_argv, env, cwd=None):
        self.created.append((session_name, list(pane_argv), dict(env)))
        # Post-F18 contract (mirrors the real wrapper): return the CANONICAL live target
        # '<session>:<window>.<pane>' — what `-P -F` prints and list_targets() keys on.
        return f"{session_name}:0.0"

    def server_env(self):
        return {}  # clean server — no leaked key

    def capture_pane(self, session_name):
        return ""

    def list_targets(self):
        return {}

    def kill(self, session_name):
        pass


def _make_adapter(tmux):
    """Build a ClaudeCodeAdapter wired to the mock tmux (the dry-run boundary)."""
    cc = _claude()
    adapter_cls = getattr(cc, "ClaudeCodeAdapter")
    try:
        return adapter_cls(tmux=tmux)
    except TypeError:
        # tolerate an attribute-injection shape
        a = adapter_cls()
        a.tmux = tmux
        return a


def _level(role_variant="L1"):
    """A LevelConfig with the given role_variant (Claude-Code runtime, Opus 5.0)."""
    return config.LevelConfig(
        level=role_variant,
        model="opus-5.0",
        runtime="claude-code",
        role_variant=role_variant,
        tool_manifest=("read", "write", "edit", "bash", "task"),
    )


def _spawn(
    adapter,
    role_variant="L1",
    address="payments/gateway/stripe#exec",
    env=None,
    brief_extra=None,
):
    brief = {
        "load_manifest": ["operational/%s/role.md" % role_variant],
        "role_variant": role_variant,
    }
    if brief_extra:
        brief.update(brief_extra)
    return adapter.pin_and_open(
        neutral_brief=brief,
        level_config=_level(role_variant),
        tmux_target=address,
        env=env if env is not None else _iso_env(),
    )


# ===========================================================================
# Module + interface presence (RED-until-built).
# ===========================================================================

def test_base_exposes_runtime_adapter_port():
    base = _base()
    assert hasattr(base, "RuntimeAdapter")
    from abc import ABC
    assert issubclass(base.RuntimeAdapter, ABC)
    assert hasattr(base.RuntimeAdapter, "pin_and_open")
    # abstract: cannot instantiate directly
    with pytest.raises(TypeError):
        base.RuntimeAdapter()


def test_claude_adapter_is_a_runtime_adapter():
    base = _base()
    cc = _claude()
    assert issubclass(cc.ClaudeCodeAdapter, base.RuntimeAdapter)


# ===========================================================================
# Part (a) — DRY-RUN argv: the COMPOSED identity prompt, not a per-level role path.
# ===========================================================================

def test_argv_uses_shared_system_prompt_file_flag(no_real_exec):
    """argv == [CC, '--system-prompt-file', <ABSOLUTE .identity-prompt.md>, ...].

    The transport increment made the flag value ABSOLUTE (resolved against HARNESS_ROOT — the
    config.py NOTE's resolution contract): the pane now boots in the NODE's workspace (-c), so a
    repo-relative path would dangle. The file begins with the shared prompt, then carries the
    selected level identity trio.
    """
    import os as _os

    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter)

    # the assembled child argv — recorded on the result and/or in the mock tmux pane_argv
    argv = _result_argv(result, tmux)
    assert "--system-prompt-file" in argv, "the boot MUST pass --system-prompt-file (H40 recipe)"
    idx = argv.index("--system-prompt-file")
    spf = argv[idx + 1]
    assert _os.path.isabs(spf), f"--system-prompt-file must be ABSOLUTE (survives any pane cwd); got {spf!r}"
    assert spf.endswith(".identity-prompt.md"), (
        "the flag value MUST be the per-spawn COMPOSED identity bundle (identity auto-load, "
        f"user ruling 2026-06-12 / LR-4); got {spf!r}"
    )


def test_system_prompt_is_not_a_per_level_role_path(no_real_exec):
    """The flag value is the SHARED prompt, NOT a per-level role/soul/config path (the key mutant)."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter, role_variant="L3")
    argv = _result_argv(result, tmux)
    spf = argv[argv.index("--system-prompt-file") + 1]
    assert "/L3/" not in spf and "role.md" not in spf and "soul.md" not in spf, (
        "the system-prompt-file must NOT be a per-level role path (e.g. operational/L3/role.md) — "
        f"it is the shared constant; got {spf!r}"
    )
    # The composed bundle CONTAINS the role text, but the argv VALUE is never a bare
    # per-level doc path — the composition is the adapter's own artifact.
    assert spf.endswith(".identity-prompt.md")


def _strip_session_id(argv):
    """Drop the per-SPAWN ``--session-id <uuid>`` pair — the ONE argv element that legitimately
    differs between spawns (it pins CC's transcript file to the recorded session_uuid; minted
    fresh per attempt). Returns (stripped_argv, session_id)."""
    argv = list(argv)
    i = argv.index("--session-id")
    sid = argv[i + 1]
    return tuple(argv[:i] + argv[i + 2:]), sid


def test_argv_identical_across_lower_producer_role_variants(no_real_exec):
    """argv is byte-identical across L2..L4 producers — the role rides the composed prompt.

    L1 keeps native Agent for its canonical intake/research seat, so it is no longer part of this
    lower-producer identity check. Review seats are checked separately because their
    reviewer-dispatch mechanics are a distinct policy surface.
    The per-spawn ``--session-id`` uuid is the ONE sanctioned exception: unique per spawn (CC
    refuses a duplicate id), so it is stripped before the identity check and pinned unique."""
    argvs, sids = [], []
    for rv in ("L2", "L3", "L4"):
        tmux = _MockTmux()
        adapter = _make_adapter(tmux)
        result = _spawn(adapter, role_variant=rv)
        stripped, sid = _strip_session_id(_result_argv(result, tmux))
        # identity AUTO-LOAD (2026-06-12): the composed per-spawn system-prompt PATH is the
        # second sanctioned per-spawn difference — strip the pair, pin its shape.
        stripped = list(stripped)
        i = stripped.index("--system-prompt-file")
        assert stripped[i + 1].endswith(".identity-prompt.md")
        stripped = tuple(stripped[:i] + stripped[i + 2:])
        argvs.append(stripped)
        sids.append(sid)
    assert len(set(argvs)) == 1, (
        f"argv (minus session-id + composed-prompt path) MUST be identical across producer "
        f"role_variants — role rides the COMPOSED FILE; got {argvs!r}"
    )
    assert len(set(sids)) == len(sids), f"--session-id must be unique per spawn; got {sids!r}"


def test_lower_producer_seats_disallow_native_agent_tool(no_real_exec):
    """L2-L4 producer seats cannot use Claude Code's native Agent as a harness child substitute."""
    for rv in ("L2", "L3", "L4"):
        tmux = _MockTmux()
        adapter = _make_adapter(tmux)
        result = _spawn(adapter, role_variant=rv)
        argv = _result_argv(result, tmux)
        assert "--disallowed-tools" in argv, f"{rv}: native Agent fence missing from argv {argv!r}"
        assert argv[argv.index("--disallowed-tools") + 1] == "Agent"


def test_l1_keeps_native_agent_for_intake_and_research(no_real_exec):
    """L1 needs native Agent for the canonical throwaway intake/research session."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter, role_variant="L1", address="L1#exec")
    argv = _result_argv(result, tmux)
    assert "--disallowed-tools" not in argv, f"L1 must keep native Agent for intake/research: {argv!r}"

    spf = argv[argv.index("--system-prompt-file") + 1]
    content = pathlib.Path(spf).read_text(encoding="utf-8")
    assert "throwaway intake grilling session" in content
    assert ".harness-outbox" in content and "child_name" in content and "child_level" in content
    assert "not a project/product child" in content


def test_review_seats_do_not_inherit_producer_native_agent_fence(no_real_exec):
    """Review-seat reviewer dispatch is a separate design surface; this producer fence stays narrow."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    lc = config.LevelConfig.for_level("L4+")
    result = adapter.pin_and_open(
        neutral_brief={"load_manifest": ["operational/L4+/role.md"], "role_variant": "L4+#review"},
        level_config=lc,
        tmux_target="proj/area/workstream#review",
        env=_iso_env(),
    )
    argv = _result_argv(result, tmux)
    assert "--disallowed-tools" not in argv, f"review seat should not inherit producer fence: {argv!r}"


def test_argv_never_carries_forbidden_flags(no_real_exec):
    """argv NEVER includes --bare / --append-system-prompt / --agents / --agent (H40 foot-guns)."""
    for rv in ("L1", "L2", "L3", "L4", "L5"):
        tmux = _MockTmux()
        adapter = _make_adapter(tmux)
        result = _spawn(adapter, role_variant=rv)
        argv = _result_argv(result, tmux)
        for flag in _FORBIDDEN_FLAGS:
            assert flag not in argv, (
                f"argv must NEVER carry {flag!r} (DAEMON §6.2: --bare forces API-key auth; "
                f"--append-system-prompt keeps full framing; --agents does not inject persona); got {argv!r}"
            )


def test_role_text_is_not_in_argv(no_real_exec):
    """Role-as-documents: the per-seat role rides the brief/load-manifest, NOT the argv/prompt.

    No argv token may carry role/persona content — only the binary, the flag, and the SHARED path.
    """
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter, role_variant="L2", address="proj/area#plan")
    argv = _result_argv(result, tmux)
    joined = " ".join(argv).lower()
    # the only path allowed is the shared system-prompt; no per-level role doc leaks into argv
    assert "operational/l2/" not in joined and "role.md" not in joined, (
        "the per-level role must arrive via the brief's load-manifest (role-as-documents), "
        f"never flattened into argv; got {argv!r}"
    )


# ===========================================================================
# Part (a) — DRY-RUN env: EXACTLY the 4-var isolation set.
# ===========================================================================

def test_env_is_exactly_the_four_isolation_vars(no_real_exec):
    """The pane env is EXACTLY the 4 isolation vars — no extra, no missing."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter)
    env = _result_env(result, tmux)
    assert frozenset(env.keys()) == _EXPECTED_ENV_KEYS, (
        f"env must be EXACTLY the 4-var isolation set {sorted(_EXPECTED_ENV_KEYS)!r}; "
        f"got {sorted(env)!r} (extra or missing var is a mutant)"
    )


def test_env_carries_no_api_key(no_real_exec):
    """The assembled env carries NO raw API key (the negative invariant holds on the real env)."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter)
    env = _result_env(result, tmux)
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    # and the guard would accept it
    assert oauth_guard.assert_no_api_key(env, ["claude", "--system-prompt-file", "x"]) is None


def test_pane_argv_is_env_i_isolated(no_real_exec):
    """The pane_argv handed to tmux.create_detached begins with the from-empty `env -i` isolator."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    _spawn(adapter)
    assert tmux.created, "create_detached must have been called (the dry-run boundary)"
    _session, pane_argv, _env = tmux.created[0]
    assert pane_argv[:2] == ["env", "-i"], (
        f"the pane command MUST be from-empty `env -i <K=V…> <argv…>`; got {pane_argv!r}"
    )
    # and the guard accepts this pane shape
    assert oauth_guard.assert_pane_env_isolated(pane_argv, server_env={}) is None


# ===========================================================================
# Part (a) — session name == addressing.session_name_for(address) (F18/OSA-01).
# The pre-fix 'harness:'+collapse(address) shape was silently RENAMED by tmux
# (':' in a session name -> '_'), so the recorded key never matched the live one.
# ===========================================================================

def test_session_name_is_the_canonical_addressing_derivation(no_real_exec):
    """The tmux session name is addressing.session_name_for(address) — 'harness-' + the address
    with '/', '#', ':', '.' folded to '-' — so tmux never renames it and reconcile can match
    tmux<->ledger (F18; the old 'harness:' prefix was rewritten to 'harness_' by tmux 3.6a)."""
    from harnessd import addressing

    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    address = "payments/gateway/stripe#exec"
    _spawn(adapter, address=address)
    assert tmux.created, "create_detached must have been called"
    session_name = tmux.created[0][0]
    assert session_name == addressing.session_name_for(address) == "harness-payments-gateway-stripe-exec", (
        f"session name must be addressing.session_name_for(address); got {session_name!r}"
    )


# ===========================================================================
# Part (a) — recorded facts: model_used, system_prompt_file(_hash), transcript_path.
# ===========================================================================

def test_records_model_used_always(no_real_exec):
    """model_used == 'opus-5.0 / claude-code' is ALWAYS recorded (config=intent, model_used=fact)."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter)
    assert result.model_used == "opus-5.0 / claude-code", (
        f"the adapter must always record the ACTUAL model_used; got {result.model_used!r}"
    )


def test_model_used_derives_from_level_config(no_real_exec):
    """model_used = '<model> / <runtime>' DERIVED from level_config (LT-8) — never a constant
    that can contradict the config. The old constant recorded one Opus/Claude-Code value even for
    a gpt-5.5/codex LevelConfig driven through this adapter, so the recorded INTENT itself was
    wrong — the deferred F17 configured-vs-actual fact-checker cannot reconcile fake intent."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    lc = config.LevelConfig(
        level="L5", model="gpt-5.5", runtime="codex", role_variant="L5#exec",
        tool_manifest=("read", "write", "edit", "bash"),
    )
    result = adapter.pin_and_open(
        neutral_brief={"role_variant": "L5#exec"},
        level_config=lc,
        tmux_target="payments/gateway/stripe#exec",
        env=_iso_env(),
    )
    assert result.model_used == "gpt-5.5 / codex", (
        f"the recorded intent must come from the CONFIG (model / runtime), got {result.model_used!r}"
    )


def test_claude_adapter_uses_the_ruled_effort_for_execution_and_review_seats(no_real_exec):
    for level, expected_effort in (
        ("L1", "xhigh"),
        ("L2", "xhigh"),
        ("L3", "xhigh"),
        ("L4", "high"),
        ("L4+", "high"),
    ):
        tmux = _MockTmux()
        adapter = _make_adapter(tmux)
        level_config = config.LevelConfig.for_level(level)
        result = adapter.pin_and_open(
            neutral_brief={
                "load_manifest": [f"operational/{level}/role.md"],
                "role_variant": level_config.role_variant,
            },
            level_config=level_config,
            tmux_target=f"proj/{level.lower()}#exec",
            env=_iso_env(),
        )
        argv = _result_argv(result, tmux)

        assert argv[argv.index("--effort") + 1] == expected_effort


def test_pinned_claude_accepts_every_generated_effort_without_model_call():
    repo_root = pathlib_Path(__file__).resolve().parents[1]
    pinned_binary = (
        repo_root
        / ".cc-pinned"
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "bin"
        / "claude.exe"
    )
    if not pinned_binary.is_file():
        pytest.skip("the pinned .cc-pinned Claude Code artifact is not present")
    assert pinned_binary.is_file()
    generated_efforts = {
        level_config.reasoning_effort
        for level_config in (
            *config.LEVEL_CONFIGS.values(),
            *config.SEMANTIC_CELL_LEVEL_CONFIGS.values(),
            *config.PRODUCT_PROBE_LEVEL_CONFIGS.values(),
        )
    }
    assert generated_efforts == {"high", "xhigh"}

    for effort in sorted(generated_efforts):
        completed = subprocess.run(
            [str(pinned_binary), "--effort", effort, "--version"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        assert "2.1.152" in completed.stdout


# ===========================================================================
# The L5 Codex-path ruling (2026-07-16): the Sol/CLIProxyAPI wiring is GONE. L5 runs GPT-5.5 on
# the native Codex runtime (see the CodexAdapter tests below); NO claude-code seat is proxied and
# NO CC pane may carry proxy/bearer vars. These tests pin the removal.
# ===========================================================================


def test_cc_adapter_has_no_proxy_machinery():
    """The proxy seams (_resolve_proxy_env / _fetch_sol_proxy_token / _PROXY_ENV_KEYS) are REMOVED,
    not dormant — pins a drift where the Sol wiring quietly returns to the CC adapter."""
    cc = _claude()
    for gone in ("_resolve_proxy_env", "_fetch_sol_proxy_token", "_PROXY_ENV_KEYS"):
        assert not hasattr(cc, gone), f"{gone} must not exist after the 2026-07-16 removal"


def test_cc_pane_refuses_stray_auth_bearer(no_real_exec):
    """A CC spawn whose env carries ANTHROPIC_AUTH_TOKEN refuses (ApiKeyForbidden) — the bearer
    has no legitimate carrier since the carve-out was removed; no actor opens."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    env = _iso_env()
    env["ANTHROPIC_AUTH_TOKEN"] = "stray-bearer"
    with pytest.raises(oauth_guard.ApiKeyForbidden):
        adapter.pin_and_open(
            neutral_brief={"role_variant": "L4"},
            level_config=config.LevelConfig.for_level("L4"),
            tmux_target="proj/widget#exec",
            env=env,
        )
    assert tmux.created == [], "no actor may open with a stray auth bearer in the pane env"


def test_native_seat_env_stays_the_isolation_floor(no_real_exec):
    """A native (Opus) CC seat's pane env is exactly the 4 isolation vars (+PATH/TERM at most) —
    no proxy vars, no bearer, nothing widened."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter, role_variant="L4")
    assert "ANTHROPIC_BASE_URL" not in result.env and "ANTHROPIC_AUTH_TOKEN" not in result.env
    _session, _pane_argv, live_env = tmux.created[0]
    assert "ANTHROPIC_AUTH_TOKEN" not in live_env and "ANTHROPIC_BASE_URL" not in live_env


def test_records_role_variant_and_system_prompt_file(no_real_exec):
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter, role_variant="L4")
    assert result.role_variant == "L4"
    assert result.system_prompt_file.endswith(".identity-prompt.md"), (
        "the recorded fact is the COMPOSED bundle path (what CC actually loaded) — "
        "identity auto-load, 2026-06-12"
    )
    assert isinstance(result.system_prompt_file_hash, str) and result.system_prompt_file_hash


def test_records_transcript_path_derived_from_session_uuid(no_real_exec):
    """transcript_path is non-null and derived from session_uuid (the spawn<->detector producer)."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = _spawn(adapter)
    assert result.session_uuid, "a session_uuid must be recorded"
    assert result.transcript_path, (
        "transcript_path must be non-null (the detector stats it; a null path breaks the contract)"
    )
    assert result.session_uuid in result.transcript_path, (
        f"transcript_path must be DERIVED from session_uuid; uuid {result.session_uuid!r} not in "
        f"path {result.transcript_path!r}"
    )
    assert result.transcript_path.endswith(".jsonl"), "transcript_path is the <session-uuid>.jsonl file"


def test_transcript_path_is_the_file_cc_actually_writes(no_real_exec, tmp_path):
    """The 2026-06-11 live-run pin: transcript_path = <config>/projects/<encoded-REALPATH-cwd>/
    <session_uuid>.jsonl with the uuid pinned into argv via --session-id. CC files transcripts by
    the pane's realpath cwd, every non-[A-Za-z0-9-] char folded to '-' — NOT by session name. A
    session-name-derived dir + an un-pinned uuid pointed verify-new-turn at a file CC never
    writes, and the idle ladder failed a healthy waiting L1 (watchdog_nonresponse)."""
    import os as _os
    import re as _re
    from pathlib import Path

    workspace = tmp_path / "nodes" / "L1_seat.dir"
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    result = adapter.pin_and_open(
        neutral_brief={
            "load_manifest": ["operational/L1/role.md"],
            "role_variant": "L1",
            "workspace": str(workspace),
        },
        level_config=_level("L1"),
        tmux_target="payments/gateway/stripe#exec",
        env=_iso_env(),
    )
    # (a) argv pins CC's session id to the RECORDED uuid (without it CC mints its own).
    argv = list(result.argv)
    assert "--session-id" in argv, "argv must pin CC's session uuid (--session-id)"
    assert argv[argv.index("--session-id") + 1] == result.session_uuid, (
        "the --session-id argv value must BE the recorded session_uuid — any other uuid means the "
        "detector stats a file CC never writes"
    )
    # (b) the directory segment is the ENCODED REALPATH cwd (CC's filing rule), never the session name.
    expected_seg = _re.sub(r"[^A-Za-z0-9-]", "-", _os.path.realpath(str(workspace)))
    expected = str(
        Path(_iso_env()["CLAUDE_CONFIG_DIR"]) / "projects" / expected_seg / f"{result.session_uuid}.jsonl"
    )
    assert result.transcript_path == expected, (
        f"transcript_path must be the encoded-realpath-cwd file CC writes;\n  got      "
        f"{result.transcript_path!r}\n  expected {expected!r}"
    )
    assert "harness-" not in result.transcript_path.split("/projects/")[1].split("/")[0] or (
        "harness-" in expected_seg
    ), "the project segment must come from the cwd, not the tmux session name"


# ===========================================================================
# Part (a) — the OAuth gate is invoked BEFORE the child runs (E32 ordering).
# ===========================================================================

def test_refuses_when_api_key_present_before_any_create_detached(no_real_exec):
    """An ANTHROPIC_API_KEY in env makes the adapter raise ApiKeyForbidden and open NO actor."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    leaky = _iso_env()
    leaky["ANTHROPIC_API_KEY"] = "nope"
    with pytest.raises(oauth_guard.ApiKeyForbidden):
        _spawn(adapter, env=leaky)
    assert tmux.created == [], (
        "the OAuth gate must fire BEFORE create_detached — NO tmux actor may open on a forbidden env"
    )


def test_refuses_when_oauth_token_absent(no_real_exec):
    """A missing CLAUDE_CODE_OAUTH_TOKEN raises AuthExpired (the DISTINCT class) and opens NO actor."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    no_tok = _iso_env()
    del no_tok["CLAUDE_CODE_OAUTH_TOKEN"]
    # absent token: with only 3 vars the env is also incomplete, but the credential check is the
    # load-bearing one — AuthExpired must surface (not ApiKeyForbidden).
    with pytest.raises(oauth_guard.AuthExpired):
        _spawn(adapter, env=no_tok)
    assert tmux.created == [], "no tmux actor may open when the OAuth token is absent (E32)"


def test_no_real_subprocess_exec_during_dry_run(no_real_exec):
    """The whole dry-run assembly executes WITHOUT a real claude.exe subprocess (no model burn)."""
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    _spawn(adapter)
    assert no_real_exec == [], "the dry-run must not have invoked any real subprocess"


def test_claude_adapter_installs_per_seat_turn_hooks_from_current_signoff(
    tmp_path, no_real_exec
):
    address = "proj/work#exec"
    workspace = tmp_path / "nodes" / "work"
    workspace.mkdir(parents=True)
    signoff = addressing.signoff_path(address, tmp_path)
    signoff.parent.mkdir(parents=True, exist_ok=True)
    signoff.write_text(
        json.dumps({"owner_token": "owner-token-from-signoff"}) + "\n",
        encoding="utf-8",
    )

    tmux = _MockTmux()
    result = _spawn(
        _make_adapter(tmux),
        address=address,
        brief_extra={
            "workspace": str(workspace),
            "turn_hook_profile": turn_state.CLAUDE_FULL_EDGES,
            "turn_runtime_root": str(tmp_path),
        },
    )

    argv = list(result.argv)
    settings_path = workspace / ".turn-hooks.exec.settings.json"
    assert argv[argv.index("--settings") + 1] == str(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
    }
    command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert str(turn_state.hook_entry_path()) in command
    assert "--owner-token owner-token-from-signoff" in command
    assert no_real_exec == []


def test_claude_adapter_reopens_the_same_session_env_uuid_it_mints(
    tmp_path, monkeypatch, no_real_exec
):
    from harnessd.spawn import blinders, sandbox

    runtime = tmp_path / "runtime"
    workspace = runtime / "nodes" / "task"
    config_dir = tmp_path / "cc-config"
    workspace.mkdir(parents=True)
    config_dir.mkdir()
    policy = blinders.derive_policy(
        node_address="proj/task#exec",
        runtime_root=str(runtime),
        bindings={"proj/task#exec": {"parent_address": None}},
        mode=blinders.ENFORCE,
        workspace=str(workspace),
        load_manifest=[],
        runtime="claude-code",
        harness_root=str(tmp_path),
    )
    containment = sandbox.resolve_containment(
        "proj/task#exec",
        runtime_root=str(runtime),
        config_dir=str(config_dir),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="claude-code",
    )
    captured = {}

    def fake_prepare(pane_argv, finalized, *, base_dir, session_name):
        captured["containment"] = finalized
        return sandbox.PreparedLaunch(
            argv=list(pane_argv),
            posture=sandbox.profile_posture(finalized, "profile"),
            applied=True,
        )

    monkeypatch.setattr(sandbox, "prepare_launch", fake_prepare)
    env = _iso_env()
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    result = _spawn(
        _make_adapter(_MockTmux()),
        address="proj/task#exec",
        env=env,
        brief_extra={
            "workspace": str(workspace),
            "containment_profile": containment,
        },
    )

    expected = str(config_dir / "session-env" / result.session_uuid)
    assert captured["containment"]["read_policy"]["runtime_inner_allows"] == [expected]
    assert result.containment_posture["runtime_inner_allows"] == [expected]
    assert no_real_exec == []


# ===========================================================================
# Part (a) — the REAL CodexAdapter (E4 — the O1 stub is retired): argv/trust/
# rollout-discovery dry-run pins, mock tmux, no real exec.
# ===========================================================================

def _codex_spawn(
    tmp_path,
    monkeypatch,
    *,
    env_extra=None,
    brief_extra=None,
    knob=False,
    rollout_extra_rows=None,
    access_exp=2_000_000_000,
    id_exp=2_000_000_000,
    return_tmux=False,
):
    """Drive the REAL CodexAdapter dry: pinned home -> tmp, fake rollout pre-created so the
    post-boot discovery resolves instantly (the REAL codex never execs)."""
    import dataclasses as _dc
    import json as _json
    import os as _os

    codex = _codex()
    home = tmp_path / "codex-home"
    _write_codex_auth(home, access_exp=access_exp, id_exp=id_exp)
    monkeypatch.setattr(
        codex_auth,
        "_preflight_access_token",
        lambda access_token, account_id: None,
    )
    monkeypatch.setattr(config, "PINNED_CODEX_HOME", str(home), raising=True)
    # the pinned binary check: point at a real file (this test file) — presence-only in v1
    monkeypatch.setattr(config, "PINNED_CODEX_BINARY", __file__, raising=True)
    monkeypatch.setattr(codex, "_harness_root", lambda: pathlib_Path("/"))

    ws = tmp_path / "nodes" / "task"
    ws.mkdir(parents=True)

    class _CodexMockTmux(_MockTmux):
        def create_detached(self, session_name, pane_argv, env, cwd=None):
            target = super().create_detached(session_name, pane_argv, env, cwd=cwd)
            worker_home = pathlib_Path(env["CODEX_HOME"])
            rollout = (
                worker_home
                / "sessions"
                / "2026"
                / "06"
                / "11"
                / "rollout-2026-06-11T00-00-00-uuid-e4.jsonl"
            )
            rollout.parent.mkdir(parents=True, exist_ok=True)
            rows = [
                {
                    "timestamp": "t",
                    "type": "session_meta",
                    "payload": {
                        "id": "uuid-e4",
                        "cwd": _os.path.realpath(str(ws)),
                        "model": "gpt-5.5",
                    },
                }
            ]
            if rollout_extra_rows is None:
                rows.append(
                    {
                        "timestamp": "t",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": "boot prompt accepted",
                        },
                    }
                )
            else:
                rows.extend(rollout_extra_rows)
            rollout.write_text("\n".join(_json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            return target

    # The Codex runtime is a DORMANT, registry-selectable capability after the 2026-07-13 L5-runtime
    # unification (LEVEL_CONFIGS["L5"] now resolves to GPT-5.6 Sol / claude-code). These tests
    # exercise the CodexAdapter directly, so they build the codex seat config INLINE (gpt-5.5 /
    # codex) rather than reading the L5 registry entry, which no longer resolves to codex.
    lc = config.LevelConfig(
        level="L5", model="gpt-5.5", runtime="codex", role_variant="L5#exec",
        tool_manifest=("read", "write", "edit", "bash"),
    )
    if knob:
        lc = _dc.replace(lc, unjailed_skip_permissions=True)
    brief = {"role_variant": "L5#exec", "workspace": str(ws)}
    if brief_extra:
        brief.update(brief_extra)
    env = {}
    if env_extra:
        env.update(env_extra)
    tmux = _CodexMockTmux()
    adapter = codex.CodexAdapter(tmux=tmux)
    result = adapter.pin_and_open(brief, lc, "proj/x#exec", env)
    if return_tmux:
        return result, home, pathlib_Path(result.env["CODEX_HOME"]), tmux
    return result, home, pathlib_Path(result.env["CODEX_HOME"])


def _fake_security(tmp_path, *, certificate: str | None):
    executable = tmp_path / "security"
    recorded_args = tmp_path / "security-args.txt"
    lines = [
        "#!/bin/sh",
        f': > "{recorded_args}"',
        f'for arg in "$@"; do printf "%s\\n" "$arg" >> "{recorded_args}"; done',
    ]
    if certificate is None:
        lines.append("exit 44")
    else:
        lines.extend(
            [
                "cat <<'EOF'",
                certificate.rstrip(),
                "EOF",
            ]
        )
    executable.write_text("\n".join(lines) + "\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable, recorded_args


from pathlib import Path as pathlib_Path


def test_codex_adapter_assembles_model_flag_and_records_facts(tmp_path, monkeypatch, no_real_exec):
    """The E32 pins: argv carries the EXPLICIT -m gpt-5.5; session_uuid + transcript_path come
    from the DISCOVERED rollout (never invented); system_prompt_file is the native-instructions
    sentinel (user decision: codex's own system message stays)."""
    result, canonical_home, worker_home = _codex_spawn(tmp_path, monkeypatch)
    argv = list(result.argv)
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5.5"
    assert result.session_uuid == "uuid-e4", "the uuid must come from the DISCOVERED rollout"
    assert result.transcript_path.endswith("rollout-2026-06-11T00-00-00-uuid-e4.jsonl")
    assert result.transcript_path.startswith(str(worker_home)), (
        "rollout discovery must read the isolated worker CODEX_HOME, not the canonical home"
    )
    assert result.model_used == "gpt-5.5 / codex"
    assert "codex-native" in result.system_prompt_file, (
        "codex runs its NATIVE base instructions (user decision) — never the CC shared prompt"
    )
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv, "knob OFF adds no bypass"
    assert result.env.get("CODEX_HOME") == str(worker_home)
    assert pathlib_Path(result.env["CODEX_HOME"]) != canonical_home
    assert result.codex_seat_id
    assert worker_home.name == result.codex_seat_id
    assert result.codex_auth_version.startswith("authv-")
    assert result.codex_access_seconds_remaining is not None
    assert no_real_exec == [], "the dry-run must not exec a real codex"


def test_codex_adapter_installs_combined_adguard_ca_bundle_in_worker_home(
    tmp_path, monkeypatch
):
    codex = _codex()
    standard_roots = tmp_path / "cert.pem"
    standard_roots.write_text(
        "-----BEGIN CERTIFICATE-----\nSTANDARD-ROOTS\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    keychain = tmp_path / "System.keychain"
    keychain.write_bytes(b"test-keychain-shape")
    adguard_pem = (
        "-----BEGIN CERTIFICATE-----\nADGUARD-PERSONAL-CA\n"
        "-----END CERTIFICATE-----\n"
    )
    security, recorded_args = _fake_security(tmp_path, certificate=adguard_pem)
    monkeypatch.setenv("HARNESSD_SECURITY", str(security))
    monkeypatch.setattr(codex, "SYSTEM_CA_BUNDLE", standard_roots, raising=False)
    monkeypatch.setattr(codex, "SYSTEM_KEYCHAIN", keychain, raising=False)

    result, _canonical_home, worker_home = _codex_spawn(tmp_path, monkeypatch)

    bundle = pathlib_Path(result.env["CODEX_CA_CERTIFICATE"])
    assert bundle == worker_home / "harness-ca-bundle.pem"
    assert bundle.is_file()
    assert bundle.stat().st_mode & 0o777 == 0o600
    content = bundle.read_text(encoding="utf-8")
    assert "STANDARD-ROOTS" in content
    assert "ADGUARD-PERSONAL-CA" in content
    assert recorded_args.read_text(encoding="utf-8").splitlines() == [
        "find-certificate",
        "-c",
        "Adguard Personal CA",
        "-p",
        str(keychain),
    ]


def test_codex_adapter_leaves_ca_environment_unchanged_without_adguard_certificate(
    tmp_path, monkeypatch
):
    codex = _codex()
    standard_roots = tmp_path / "cert.pem"
    standard_roots.write_text(
        "-----BEGIN CERTIFICATE-----\nSTANDARD-ROOTS\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    keychain = tmp_path / "System.keychain"
    keychain.write_bytes(b"test-keychain-shape")
    security, _recorded_args = _fake_security(tmp_path, certificate=None)
    monkeypatch.setenv("HARNESSD_SECURITY", str(security))
    monkeypatch.setattr(codex, "SYSTEM_CA_BUNDLE", standard_roots, raising=False)
    monkeypatch.setattr(codex, "SYSTEM_KEYCHAIN", keychain, raising=False)

    result, _canonical_home, worker_home = _codex_spawn(tmp_path, monkeypatch)

    assert "CODEX_CA_CERTIFICATE" not in result.env
    assert not (worker_home / "harness-ca-bundle.pem").exists()


def test_codex_adapter_installs_owner_fenced_turn_end_notify(
    tmp_path, monkeypatch, no_real_exec
):
    address = "proj/x#exec"
    signoff = addressing.signoff_path(address, tmp_path)
    signoff.parent.mkdir(parents=True, exist_ok=True)
    signoff.write_text(
        json.dumps({"owner_token": "codex-owner-from-signoff"}) + "\n",
        encoding="utf-8",
    )
    result, _canonical_home, worker_home = _codex_spawn(
        tmp_path,
        monkeypatch,
        brief_extra={
            "turn_hook_profile": turn_state.CODEX_TURN_END_ONLY,
            "turn_runtime_root": str(tmp_path),
        },
    )
    config_payload = tomllib.loads(
        (worker_home / "config.toml").read_text(encoding="utf-8")
    )
    notify = config_payload["notify"]
    assert notify[0]
    assert notify[1] == str(turn_state.hook_entry_path())
    assert notify[notify.index("--node-address") + 1] == address
    assert notify[notify.index("--owner-token") + 1] == "codex-owner-from-signoff"
    assert notify[notify.index("--runtime") + 1] == "codex"
    assert no_real_exec == []


def test_codex_launch_packet_uses_boot_prompt_file_not_large_tmux_argv(tmp_path, monkeypatch, no_real_exec):
    launch = tmp_path / "launch.md"
    launch.write_text(
        "# Launch Packet\n\nThis is the minimal L5 executor surface.\n",
        encoding="utf-8",
    )

    result, _canonical_home, _worker_home, tmux = _codex_spawn(
        tmp_path,
        monkeypatch,
        brief_extra={
            "launch_packet_file": str(launch),
            "launch_packet_hash": "abc123",
            "launch_surface_source_hash": "source123",
            "reference_map_file": str(tmp_path / "reference.md"),
            "reference_map_hash": "def456",
            "reference_map_json_file": str(tmp_path / "reference.json"),
            "launch_surface_version": "launch-surface-v1",
        },
        return_tmux=True,
    )

    boot_prompt_file = tmp_path / "nodes" / "task" / ".codex-boot-prompt.md"
    assert boot_prompt_file.is_file()
    boot_prompt = boot_prompt_file.read_text(encoding="utf-8")
    assert "This is the minimal L5 executor surface." in boot_prompt
    assert "First, read these identity documents" not in boot_prompt

    _session_name, pane_argv, _env = tmux.created[0]
    joined_pane = "\n".join(pane_argv)
    assert "This is the minimal L5 executor surface." not in joined_pane
    assert str(boot_prompt_file) in pane_argv
    assert "/bin/sh" in pane_argv
    assert result.argv[-1] == str(boot_prompt_file)
    assert result.launch_packet_file == str(launch)
    assert result.launch_surface_source_hash == "source123"
    assert result.reference_map_json_file == str(tmp_path / "reference.json")


def test_codex_adapter_seeds_isolated_worker_home_without_canonical_refresh_token(
    tmp_path, monkeypatch, no_real_exec
):
    """Each Codex worker gets isolated mutable state and no usable refresh token."""
    result, canonical_home, worker_home = _codex_spawn(tmp_path, monkeypatch)
    canonical_refresh = json.loads((canonical_home / "auth.json").read_text(encoding="utf-8"))[
        "tokens"
    ]["refresh_token"]
    child_text = (worker_home / "auth.json").read_text(encoding="utf-8")
    child_auth = json.loads(child_text)

    assert pathlib_Path(result.env["CODEX_HOME"]) == worker_home
    assert child_auth["OPENAI_API_KEY"] is None
    assert child_auth["tokens"]["refresh_token"] == ""
    assert canonical_refresh not in child_text
    assert (worker_home / "config.toml").is_file()


def test_codex_adapter_generated_config_disables_prompts_and_sets_high_effort(
    tmp_path, monkeypatch, no_real_exec
):
    """Spawned Codex seats never prompt or inherit low effort, without weakening containment."""
    _result, _canonical_home, worker_home = _codex_spawn(tmp_path, monkeypatch)

    payload = tomllib.loads((worker_home / "config.toml").read_text(encoding="utf-8"))

    assert payload["approval_policy"] == "never"
    assert payload["model_reasoning_effort"] == "high"
    assert "sandbox_mode" not in payload


def test_pinned_codex_parses_generated_noninteractive_seat_config(
    tmp_path, monkeypatch
):
    """The production pin accepts the exact generated config without a model call."""
    repo_root = pathlib_Path(__file__).resolve().parents[1]
    pinned_binary = repo_root / config.PINNED_CODEX_BINARY
    if not pinned_binary.is_file():
        pytest.skip("the pinned .codex-pinned Codex artifact is not present")
    assert pinned_binary.is_file()
    _result, _canonical_home, worker_home = _codex_spawn(tmp_path, monkeypatch)
    payload = tomllib.loads((worker_home / "config.toml").read_text(encoding="utf-8"))
    assert payload["approval_policy"] == "never"
    assert payload["model_reasoning_effort"] == "high"
    assert "sandbox_mode" not in payload

    completed = subprocess.run(
        [str(pinned_binary), "features", "list"],
        cwd=repo_root,
        env={**os.environ, "CODEX_HOME": str(worker_home)},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_codex_adapter_allows_expired_id_token_when_access_token_is_fresh(
    tmp_path, monkeypatch, no_real_exec
):
    """The worker turn is gated by access-token freshness; id_token is presence-only."""
    result, _canonical_home, _worker_home = _codex_spawn(
        tmp_path,
        monkeypatch,
        access_exp=2_000_000_000,
        id_exp=1_000,
    )

    assert result.model_used == "gpt-5.5 / codex"
    assert result.codex_access_seconds_remaining is not None


def test_codex_adapter_knob_on_adds_bypass_and_seeds_trust(tmp_path, monkeypatch, no_real_exec):
    """unjailed_skip_permissions -> --dangerously-bypass-approvals-and-sandbox (probed: YOLO
    mode), posture journaled; the cwd is trust-seeded realpath-keyed in the worker config.toml."""
    import os as _os
    result, _canonical_home, worker_home = _codex_spawn(tmp_path, monkeypatch, knob=True)
    assert "--dangerously-bypass-approvals-and-sandbox" in result.argv
    assert result.permission_posture == "unjailed-skip-permissions-override"
    cfg = (worker_home / "config.toml").read_text(encoding="utf-8")
    ws_real = _os.path.realpath(str(tmp_path / "nodes" / "task"))
    assert f'[projects."{ws_real}"]' in cfg and 'trust_level = "trusted"' in cfg, (
        "the pane cwd must be deterministically trust-seeded (realpath-keyed — the probe result)"
    )


def test_codex_adapter_expired_canonical_auth_fails_before_tmux(tmp_path, monkeypatch):
    """A stale canonical Codex token is a pre-spawn auth_expired failure, not a half-open pane."""
    codex = _codex()
    home = tmp_path / "codex-home"
    _write_codex_auth(home, access_exp=1_000, id_exp=1_000)
    monkeypatch.setattr(config, "PINNED_CODEX_HOME", str(home), raising=True)
    monkeypatch.setattr(config, "PINNED_CODEX_BINARY", __file__, raising=True)
    monkeypatch.setattr(codex, "_harness_root", lambda: pathlib_Path("/"))
    ws = tmp_path / "nodes" / "task"
    ws.mkdir(parents=True)
    tmux = _MockTmux()
    adapter = codex.CodexAdapter(tmux=tmux)

    with pytest.raises(oauth_guard.SpawnFailure) as exc:
        adapter.pin_and_open(
            {"role_variant": "L5#exec", "workspace": str(ws)},
            # Inline codex seat config (dormant capability) — the L5 registry entry now resolves to
            # GPT-5.6 Sol / claude-code after the 2026-07-13 unification.
            config.LevelConfig(
                level="L5", model="gpt-5.5", runtime="codex", role_variant="L5#exec",
                tool_manifest=("read", "write", "edit", "bash"),
            ),
            "proj/x#exec",
            {},
        )

    assert getattr(exc.value, "failure_class", "") == "auth_expired"
    assert tmux.created == []


def test_codex_adapter_refuses_openai_key(tmp_path, monkeypatch):
    """The shared negative invariant survives the real fill: OPENAI_API_KEY in env refuses."""
    with pytest.raises(oauth_guard.ApiKeyForbidden):
        _codex_spawn(tmp_path, monkeypatch, env_extra={"OPENAI_API_KEY": "sk-nope"})


def _codex_containment(tmp_path, *, mode="observe"):
    from harnessd.spawn import sandbox

    runtime = tmp_path / "runtime"
    workspace = tmp_path / "nodes" / "task"
    policy = {
        "version": "blinders-v1",
        "mode": mode,
        "node_address": "proj/x#exec",
        "l1_god_view": False,
        "allow_subpaths": [str(workspace)],
        "allow_literals": ["/"],
        "direct_surfaces": [],
        "declared_documents": [],
        "runtime": "codex",
        "runtime_state_roots": [],
        "runtime_inner_denies": [],
    }
    return sandbox.resolve_containment(
        "proj/x#exec",
        runtime_root=str(runtime),
        config_dir=str(workspace / ".codex-placeholder"),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="codex",
    )


def test_codex_adapter_disables_its_own_sandbox_when_externally_jailed(tmp_path, monkeypatch):
    """An externally jailed Codex seat MUST NOT try to apply its own sandbox (CELL-NESTED-SANDBOX).

    macOS refuses a second ``sandbox_apply`` from a process already under a restrictive seatbelt —
    that refusal is itself a ratified escape-block (``test_nested_sandbox_exec_blocked``). So a
    Codex seat wrapped in the harness profile must be told it is externally sandboxed, exactly as
    the flag's own help text intends, or EVERY command and patch dies with
    ``sandbox-exec: sandbox_apply: Operation not permitted`` — the run-5 live failure. This is the
    Codex mirror of the Claude adapter's ``jailed-skip-permissions`` posture and is independent of
    blinders mode: the write jail that blocks nesting is present in observe too.
    """
    containment = _codex_containment(tmp_path)
    result, _canonical, _worker, tmux = _codex_spawn(
        tmp_path,
        monkeypatch,
        brief_extra={"containment_profile": containment},
        return_tmux=True,
    )

    _session, pane_argv, _env = tmux.created[0]
    assert "--dangerously-bypass-approvals-and-sandbox" in pane_argv, (
        "a jailed Codex seat must run with its OWN sandbox off; the harness seatbelt is the "
        f"containment: {pane_argv!r}"
    )
    assert result.permission_posture == "externally-jailed-bypass"


def test_codex_adapter_keeps_its_own_sandbox_when_unjailed(tmp_path, monkeypatch, no_real_exec):
    """No external jail -> no bypass. The flag is granted BY containment, never by default."""
    result, _canonical, _worker, tmux = _codex_spawn(
        tmp_path, monkeypatch, return_tmux=True
    )
    _session, pane_argv, _env = tmux.created[0]
    assert "--dangerously-bypass-approvals-and-sandbox" not in pane_argv
    assert result.permission_posture == "unjailed-prompting"


def test_codex_adapter_wraps_the_unchanged_inner_pane_with_external_containment(
    tmp_path, monkeypatch
):
    """Codex now shares Claude's external physics; its exact worker home is bound at spawn."""
    from harnessd.spawn import sandbox

    runtime = tmp_path / "runtime"
    workspace = tmp_path / "nodes" / "task"
    policy = {
        "version": "blinders-v1",
        "mode": "observe",
        "node_address": "proj/x#exec",
        "l1_god_view": False,
        "allow_subpaths": [str(workspace)],
        "allow_literals": ["/"],
        "direct_surfaces": [],
        "declared_documents": [],
        "runtime": "codex",
        "runtime_state_roots": [],
        "runtime_inner_denies": [],
    }
    containment = sandbox.resolve_containment(
        "proj/x#exec",
        runtime_root=str(runtime),
        config_dir=str(workspace / ".codex-placeholder"),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="codex",
    )
    result, canonical_home, worker_home, tmux = _codex_spawn(
        tmp_path,
        monkeypatch,
        brief_extra={"containment_profile": containment},
        return_tmux=True,
    )

    _session, pane_argv, _env = tmux.created[0]
    assert pane_argv[0].endswith("sandbox-exec")
    assert "/bin/sh" in pane_argv
    assert result.containment_posture["mode"] == "observe"
    assert result.containment_posture["runtime_state_roots"] == [str(worker_home)]
    assert str(canonical_home) not in result.containment_posture["runtime_state_roots"]


def test_codex_adapter_auth_error_in_rollout_fails_as_auth_expired(tmp_path, monkeypatch):
    """A discovered rollout is not enough: if the first turn records the Codex
    unauthorized refresh-token failure, the adapter must fail spawn as
    auth_expired instead of letting watchdog rediscover it as nonresponse."""
    with pytest.raises(oauth_guard.SpawnFailure) as exc:
        _codex_spawn(
            tmp_path,
            monkeypatch,
            rollout_extra_rows=[
                {
                    "timestamp": "2026-06-16T05:19:01.388Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "error",
                        "message": (
                            "Your access token could not be refreshed because your "
                            "refresh token was already used. Please log out and sign in again."
                        ),
                        "codex_error_info": "unauthorized",
                    },
                }
            ],
        )
    assert getattr(exc.value, "failure_class", "") == "auth_expired"


def test_codex_adapter_rate_limit_in_rollout_fails_as_auth_rate_limited(tmp_path, monkeypatch):
    """A spawn-window Codex account/rate-limit error is infrastructure capacity,
    not a healthy spawn and not a login-expired class."""
    with pytest.raises(oauth_guard.SpawnFailure) as exc:
        _codex_spawn(
            tmp_path,
            monkeypatch,
            rollout_extra_rows=[
                {
                    "timestamp": "2026-06-16T05:20:01.388Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "error",
                        "message": "Too many requests. Please try again later.",
                        "codex_error_info": "rate_limit_exceeded",
                    },
                }
            ],
        )
    assert getattr(exc.value, "failure_class", "") == "auth_rate_limited"


def test_codex_discovery_fail_loud_on_no_rollout(tmp_path, monkeypatch):
    """No matching rollout within the deadline -> SpawnFailure('transcript_undiscovered') — a
    blind binding is the 2026-06-11 watchdog-blindness class; refuse, never guess."""
    import os as _os
    codex = _codex()
    empty = tmp_path / "empty-sessions"
    empty.mkdir()
    with pytest.raises(Exception) as exc:
        codex.CodexAdapter.discover_rollout(
            empty, _os.path.realpath(str(tmp_path)), 0.0, deadline_s=0.2, poll_s=0.05
        )
    assert getattr(exc.value, "failure_class", "") == "transcript_undiscovered"


# ===========================================================================
# Helpers to read the assembled argv/env off the result OR the mock tmux record.
# The frozen contract: the assembled child argv (the part AFTER `env -i <K=V…>`) and the
# 4-var env are observable. We accept either an explicit result.argv/result.env OR the
# pane_argv recorded by the mock tmux (env -i <K=V…> <argv…>), recovering both.
# ===========================================================================

def _recover_from_pane_argv(pane_argv):
    """Split `env -i <K=V…> <argv…>` into (env_dict, child_argv)."""
    assert pane_argv[:2] == ["env", "-i"], (
        f"pane_argv must begin with the from-empty isolator `env -i`; got {pane_argv!r}"
    )
    env = {}
    i = 2
    while i < len(pane_argv) and "=" in pane_argv[i] and not pane_argv[i].startswith("-"):
        k, v = pane_argv[i].split("=", 1)
        env[k] = v
        i += 1
    return env, pane_argv[i:]


def _result_argv(result, tmux):
    argv = getattr(result, "argv", None)
    if argv:
        return list(argv)
    assert tmux.created, "no argv on result and create_detached not called — cannot read child argv"
    _session, pane_argv, _env = tmux.created[0]
    _env_recovered, child_argv = _recover_from_pane_argv(pane_argv)
    return list(child_argv)


def _result_env(result, tmux):
    env = getattr(result, "env", None)
    if env:
        return dict(env)
    assert tmux.created, "no env on result and create_detached not called — cannot read env"
    _session, pane_argv, _passed_env = tmux.created[0]
    if _passed_env:
        return dict(_passed_env)
    env_recovered, _child = _recover_from_pane_argv(pane_argv)
    return env_recovered


# ===========================================================================
# Identity AUTO-LOAD (LR-4 cure; user ruling 2026-06-12 amending H40 Decision B):
# the per-level identity trio is FLATTENED into a per-spawn system-prompt file —
# identity arrives in context before the first token, never riding agent diligence.
# ===========================================================================

def test_argv_system_prompt_is_the_composed_identity_bundle(no_real_exec, tmp_path):
    """The adapter composes shared system-prompt + the level's soul/role/config into
    <workspace>/.identity-prompt.md and argv points at IT; the recorded facts carry the
    composed path + the hash of the COMPOSED content. (Mutant: shared-constant argv
    restored -> identity back on agent diligence -> caught.)"""
    import hashlib
    import pathlib

    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    ws = tmp_path / "node-ws"
    result = adapter.pin_and_open(
        neutral_brief={"load_manifest": ["operational/L3/role.md"], "role_variant": "L3",
                       "workspace": str(ws)},
        level_config=_level("L3"),
        tmux_target="payments/gateway/stripe#exec",
        env=_iso_env(),
    )
    argv = _result_argv(result, tmux)
    spf = argv[argv.index("--system-prompt-file") + 1]
    assert spf.endswith(".identity-prompt.md"), (
        f"argv must point at the per-spawn COMPOSED identity prompt; got {spf!r}"
    )
    composed = pathlib.Path(spf)
    assert composed.is_file(), "the composed identity prompt must exist on disk at spawn time"
    content = composed.read_text(encoding="utf-8")

    cc_mod = _claude()
    root = cc_mod._harness_root()
    shared = (root / config.SYSTEM_PROMPT_FILE).read_text(encoding="utf-8")
    assert shared.strip()[:80] in content, "the shared system prompt leads the composed bundle"
    for rel in ("operational/L3/soul.md", "operational/L3/role.md", "operational/L3/config.md"):
        body = (root / rel).read_text(encoding="utf-8")
        assert body.strip()[:60] in content, f"{rel} must be FLATTENED into the identity prompt"
        assert rel in content, f"the bundle must carry the provenance header for {rel}"
    assert ".harness-outbox" in content and "child_name" in content and "child_level" in content, (
        "producer seats must receive the sanctioned harness child-spawn path in their composed "
        "identity prompt, not only as a buried load-manifest reference"
    )
    assert "runtime-native `Agent`" in content, (
        "the composed identity prompt must explain that native Agent is not a product-work "
        "delegation path"
    )

    assert result.system_prompt_file == spf, "the recorded fact is the COMPOSED path (what CC loaded)"
    assert result.system_prompt_file_hash == hashlib.sha256(content.encode("utf-8")).hexdigest(), (
        "the hash fact covers the COMPOSED content"
    )


def test_claude_launch_packet_replaces_full_identity_trio_for_pilot_seat(no_real_exec, tmp_path):
    tmux = _MockTmux()
    adapter = _make_adapter(tmux)
    ws = tmp_path / "node-ws"
    launch = ws / ".launch-packet.md"
    ws.mkdir(parents=True)
    launch.write_text(
        "# Launch Packet\n\nThis is the minimal L5+ review surface.\n",
        encoding="utf-8",
    )

    result = adapter.pin_and_open(
        neutral_brief={
            "role_variant": "L5+#review",
            "workspace": str(ws),
            "launch_packet_file": str(launch),
            "launch_packet_hash": "abc123",
            "launch_surface_source_hash": "source123",
            "reference_map_file": str(ws / ".reference-map.md"),
            "reference_map_hash": "def456",
            "reference_map_json_file": str(ws / ".reference-map.json"),
            "launch_surface_version": "launch-surface-v1",
        },
        level_config=_level("L5+"),
        tmux_target="proj/widget#review",
        env=_iso_env(),
    )

    content = pathlib_Path(result.system_prompt_file).read_text(encoding="utf-8")
    assert "This is the minimal L5+ review surface." in content
    assert "operational/L5+/soul.md" not in content
    assert "operational/L5+/role.md" not in content
    assert "Identity — Load These Documents" not in content
    assert result.launch_packet_file == str(launch)
    assert result.launch_surface_source_hash == "source123"
    assert result.reference_map_json_file == str(ws / ".reference-map.json")


def test_codex_boot_prompt_lists_identity_docs_and_plan_first(tmp_path, monkeypatch, no_real_exec):
    """Codex gets NO system-prompt injection (user decision: native instructions stay) — its
    identity auto-load rides the BOOT PROMPT: the trio listed as explicit paths to read first,
    plus task-list-first / durable-plan discipline. (Mutant: bare 'read brief.md' boot prompt ->
    identity unloaded -> caught.)"""
    result, _canonical_home, _worker_home = _codex_spawn(tmp_path, monkeypatch)
    prompt_path = pathlib_Path(list(result.argv)[-1])
    assert prompt_path.name == ".codex-boot-prompt.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    for rel in ("operational/L5/soul.md", "operational/L5/role.md", "operational/L5/config.md"):
        assert rel in prompt, f"the codex boot prompt must list {rel} to read FIRST; got {prompt!r}"
    assert "brief.md" in prompt
    assert "native plan tool" in prompt
    assert "plan.md" in prompt, "the boot prompt carries the durable-plan discipline"
