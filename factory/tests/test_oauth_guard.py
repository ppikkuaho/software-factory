"""Increment 8 — FROZEN acceptance for the OAuth-only guard (the HARD INVARIANT).

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.11 — the `oauth_guard.py` frozen interface (the four functions +
    the two exception classes `ApiKeyForbidden` / `AuthExpired`).
  * IMPLEMENTATION-PLAN §7 + the Increment-8 Done-test (lines 726-732, 1005-1008).
  * DAEMON §7 (genesis credential precondition) + §6.3 (auth_expired as a DISTINCT class so a
    token lapse reads as "refresh the token", NOT a fleet-wide model-outage storm).

THE INVARIANT: no part of the system may ever use a raw API key; all model usage rides the OAuth
subscription token. The negative invariant (`assert_no_api_key`) is RUNTIME-AGNOSTIC — always on for
Claude AND Codex — so a future Codex adapter fill cannot satisfy the gate by deleting a check.

This is the PURE env/argv unit (Increment 8). The REAL-tmux pane-env-leak assertion is Increment 9.
These are pure functions over REAL env dicts + argv lists — inherently real inputs, NO mock needed.

RED until the builder creates `harnessd/spawn/__init__.py` + `harnessd/spawn/oauth_guard.py`.

Load-bearing design (each test pins a specific mutant from the plan's mutant hints):
  * ANTHROPIC_API_KEY alone raises          (mutant: check only OPENAI  -> caught)
  * OPENAI_API_KEY alone raises             (mutant: check only ANTHROPIC -> caught)
  * --bare alone raises                     (mutant: drop the --bare check -> caught)
  * non-env-i pane raises                   (mutant: skip the isolator check -> caught)
  * api key in server_env raises            (mutant: skip the server-env check -> caught)
  * absent OAuth token raises AuthExpired   (mutant: do not raise -> caught)
  * absent token raises AuthExpired SPECIFICALLY, not ApiKeyForbidden
                                            (mutant: raise ApiKeyForbidden instead -> caught;
                                             the class distinction is load-bearing)
"""

import importlib

import pytest


# --------------------------------------------------------------------------------------------------
# Module + interface presence (RED-until-built). The whole module is the test target — importing it
# is the first RED signal until harnessd/spawn/oauth_guard.py exists.
# --------------------------------------------------------------------------------------------------

def _guard():
    """Import the module under test fresh each call (no stale binding across edits)."""
    return importlib.import_module("harnessd.spawn.oauth_guard")


def test_spawn_is_a_real_package():
    """harnessd.spawn must be a real (regular) package — the builder adds
    harnessd/spawn/__init__.py, not just a namespace dir."""
    pkg = importlib.import_module("harnessd.spawn")
    assert getattr(pkg, "__file__", None) is not None, (
        "harnessd.spawn is an implicit namespace dir, not a real package — "
        "the builder must add harnessd/spawn/__init__.py"
    )
    assert pkg.__spec__.origin not in (None, "namespace"), (
        "harnessd.spawn resolved as a namespace package; a real __init__.py is required"
    )


def test_oauth_guard_exposes_the_frozen_interface():
    """§2.11: the four functions + the two exception classes are all present."""
    g = _guard()
    for name in (
        "assert_no_api_key",
        "assert_pane_env_isolated",
        "assert_oauth_only",
        "check_credential_health",
        "ApiKeyForbidden",
        "AuthExpired",
    ):
        assert hasattr(g, name), f"oauth_guard must expose `{name}` (§2.11 frozen interface)"


# --------------------------------------------------------------------------------------------------
# Pinned, REAL inputs (no mock). The one clean OAuth env the system actually boots with.
# --------------------------------------------------------------------------------------------------

# The exact 4-var isolation env the Claude adapter assembles (§2.11 / §7) — OAuth token, NO api key.
PINNED_OAUTH_ENV = {
    "CLAUDE_CONFIG_DIR": "/harness/.cc-pinned/config",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-subscription-token-not-an-api-key",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}

# The clean assembled argv the adapter builds — never --bare / --append-system-prompt / --agents.
CLEAN_ARGV = [
    "/harness/.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "--system-prompt-file",
    "operational/shared/system-prompt.md",
]

# A from-empty (env -i) isolated pane command — the load-bearing tmux isolator (§7).
ISOLATED_PANE_ARGV = [
    "env",
    "-i",
    "CLAUDE_CONFIG_DIR=/harness/.cc-pinned/config",
    "CLAUDE_CODE_OAUTH_TOKEN=sk-oauth-subscription-token-not-an-api-key",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
    "DISABLE_AUTOUPDATER=1",
    "/harness/.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "--system-prompt-file",
    "operational/shared/system-prompt.md",
]

# A clean tmux SERVER env — no stray ANTHROPIC/OPENAI key leaking from the long-lived server.
CLEAN_SERVER_ENV = {
    "PATH": "/usr/bin:/bin",
    "TMUX": "/tmp/tmux-501/default,12345,0",
    "TERM": "screen-256color",
}


# ==================================================================================================
# 1. assert_no_api_key — the RUNTIME-AGNOSTIC negative invariant (always on, Claude AND Codex).
#    Each forbidden path asserted INDEPENDENTLY so each mutant is caught in isolation.
# ==================================================================================================

def test_no_api_key_passes_on_clean_oauth_env():
    """PASSES on the pinned clean OAuth env (token only, no api key, no --bare)."""
    g = _guard()
    # No exception — a clean OAuth spawn is allowed.
    assert g.assert_no_api_key(dict(PINNED_OAUTH_ENV), list(CLEAN_ARGV)) is None


def test_no_api_key_raises_on_anthropic_api_key_ALONE():
    """ANTHROPIC_API_KEY present (and OPENAI absent, no --bare) RAISES ApiKeyForbidden.

    Mutant pinned: a guard that checks ONLY OPENAI_API_KEY would let this through -> caught here.
    """
    g = _guard()
    env = dict(PINNED_OAUTH_ENV)
    env["ANTHROPIC_API_KEY"] = "test-ant-raw-api-key"
    assert "OPENAI_API_KEY" not in env
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_no_api_key(env, list(CLEAN_ARGV))


def test_no_api_key_raises_on_openai_api_key_ALONE():
    """OPENAI_API_KEY present (and ANTHROPIC absent, no --bare) RAISES ApiKeyForbidden.

    Mutant pinned: a guard that checks ONLY ANTHROPIC_API_KEY would let this through -> caught here.
    """
    g = _guard()
    env = dict(PINNED_OAUTH_ENV)
    env["OPENAI_API_KEY"] = "sk-openai-raw-api-key"
    assert "ANTHROPIC_API_KEY" not in env
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_no_api_key(env, list(CLEAN_ARGV))


def test_no_api_key_raises_on_bare_flag_ALONE():
    """`--bare` in argv (env clean, no api key) RAISES ApiKeyForbidden.

    --bare is the H40 foot-gun: it forces ANTHROPIC_API_KEY auth and breaks the OAuth token.
    Mutant pinned: dropping the --bare check would let a clean-env+--bare spawn through -> caught.
    """
    g = _guard()
    env = dict(PINNED_OAUTH_ENV)
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    argv = list(CLEAN_ARGV) + ["--bare"]
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_no_api_key(env, argv)


def test_no_api_key_bare_detected_regardless_of_position():
    """--bare anywhere in argv (not only appended) is forbidden — pins a positional-only mutant."""
    g = _guard()
    argv = [CLEAN_ARGV[0], "--bare", "--system-prompt-file", "operational/shared/system-prompt.md"]
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_no_api_key(dict(PINNED_OAUTH_ENV), argv)


def test_no_api_key_is_RUNTIME_AGNOSTIC_shared_invariant():
    """The negative invariant applies REGARDLESS of runtime — it is the SHARED, immovable gate that a
    future Codex adapter fill cannot delete (§7).

    There is NO runtime parameter on assert_no_api_key by design (it is runtime-agnostic). We assert
    the contract holds identically whether the env carries a Claude hint or a Codex hint: an api key
    is forbidden in EITHER world, and a clean env passes in EITHER world.
    """
    g = _guard()
    # Claude-flavoured env with a stray key -> forbidden.
    claude_env = dict(PINNED_OAUTH_ENV, ANTHROPIC_API_KEY="leak")
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_no_api_key(claude_env, list(CLEAN_ARGV))
    # Codex-flavoured env (no Claude OAuth token at all) with a stray key -> STILL forbidden,
    # i.e. the gate is not Claude-gated; it fires for the Codex world too.
    codex_env = {"CODEX_HOME": "/x", "OPENAI_API_KEY": "leak"}
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_no_api_key(codex_env, list(CLEAN_ARGV))
    # And a Codex env WITHOUT any api key passes the negative gate (no positive-token requirement
    # in the shared gate — the positive check is per-adapter, not here).
    clean_codex_env = {"CODEX_HOME": "/x"}
    assert g.assert_no_api_key(clean_codex_env, list(CLEAN_ARGV)) is None


def test_no_api_key_signature_is_env_and_argv_only():
    """§2.11 signature: the params are exactly (env, argv) — the raw-key / --bare hard invariant
    is runtime-agnostic and NOT gated on a runtime/adapter/seat arg. (The 2026-07-13 keyword-only
    ``proxied_seat`` carve-out flag was REMOVED 2026-07-16 with the L5 Codex-path ruling: no seat
    is proxied, so no carve-out exists to thread.) Pins a drift where someone re-adds a seat-scoped
    allowance or gates the raw-key invariant on a runtime arg."""
    import inspect
    g = _guard()
    sig = inspect.signature(g.assert_no_api_key)
    assert list(sig.parameters) == ["env", "argv"], (
        f"assert_no_api_key's params must be exactly (env, argv); got {list(sig.parameters)}"
    )


# ==================================================================================================
# 2. assert_pane_env_isolated — closes the tmux-server env-leak (the from-empty isolator gate).
# ==================================================================================================

def test_pane_env_isolated_passes_on_env_i_pane_and_clean_server():
    """PASSES when pane_argv begins with the from-empty isolator (`env -i ...`) AND the tmux SERVER
    env carries no api key."""
    g = _guard()
    assert g.assert_pane_env_isolated(list(ISOLATED_PANE_ARGV), dict(CLEAN_SERVER_ENV)) is None


def test_pane_env_isolated_raises_when_pane_NOT_env_i_isolated():
    """A pane launched WITHOUT the from-empty wrapper (bare `tmux new-session <cmd>` style — inherits
    the server env) RAISES ApiKeyForbidden, even with a clean server env.

    Mutant pinned: skipping the isolator check would let an inheriting pane through -> caught here.
    """
    g = _guard()
    # Same command, but NOT prefixed by `env -i ...` — it would inherit the tmux server environment.
    non_isolated = [
        "/harness/.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
        "--system-prompt-file",
        "operational/shared/system-prompt.md",
    ]
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_pane_env_isolated(non_isolated, dict(CLEAN_SERVER_ENV))


def test_pane_env_isolated_raises_on_bare_env_without_i_flag():
    """`env <vars> cmd` WITHOUT the `-i` from-empty flag is NOT isolation (it augments, not clears).
    Pins a mutant that matches on the `env` token alone and ignores the `-i`."""
    g = _guard()
    not_from_empty = [
        "env",
        "CLAUDE_CODE_OAUTH_TOKEN=tok",  # no `-i` -> inherits the server env, then adds
        "/harness/.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    ]
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_pane_env_isolated(not_from_empty, dict(CLEAN_SERVER_ENV))


def test_pane_env_isolated_raises_on_anthropic_key_in_SERVER_env():
    """An ANTHROPIC_API_KEY sitting in the tmux SERVER env RAISES — even when the pane IS env-i
    isolated (the guard checks the env the pane could inherit FROM, not just the pane prefix).

    Mutant pinned: skipping the server-env check would pass on the isolated prefix alone -> caught.
    """
    g = _guard()
    leaky_server = dict(CLEAN_SERVER_ENV, ANTHROPIC_API_KEY="test-ant-leaked-into-server")
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_pane_env_isolated(list(ISOLATED_PANE_ARGV), leaky_server)


def test_pane_env_isolated_raises_on_openai_key_in_SERVER_env():
    """An OPENAI_API_KEY in the tmux SERVER env RAISES too (both api keys are checked in the server
    env, not only ANTHROPIC) — pins a check-only-ANTHROPIC-in-server mutant."""
    g = _guard()
    leaky_server = dict(CLEAN_SERVER_ENV, OPENAI_API_KEY="sk-openai-leaked-into-server")
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_pane_env_isolated(list(ISOLATED_PANE_ARGV), leaky_server)


# ==================================================================================================
# 3. assert_oauth_only — the composed adapter call (no_api_key + pane_env_isolated).
# ==================================================================================================

def test_oauth_only_passes_on_fully_clean_inputs():
    """The composed gate passes when env+argv are clean AND the pane is env-i isolated AND the
    server env is clean."""
    g = _guard()
    assert g.assert_oauth_only(
        dict(PINNED_OAUTH_ENV), list(CLEAN_ARGV), list(ISOLATED_PANE_ARGV), dict(CLEAN_SERVER_ENV)
    ) is None


def test_oauth_only_raises_when_negative_invariant_trips():
    """assert_oauth_only must COMPOSE assert_no_api_key — a stray api key in env raises even though
    the pane/server inputs are clean (pins a composition that forgot the no_api_key half)."""
    g = _guard()
    env = dict(PINNED_OAUTH_ENV, OPENAI_API_KEY="leak")
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_oauth_only(env, list(CLEAN_ARGV), list(ISOLATED_PANE_ARGV), dict(CLEAN_SERVER_ENV))


def test_oauth_only_raises_when_bare_in_argv():
    """assert_oauth_only also catches --bare (the no_api_key half covers argv) on otherwise-clean
    inputs — pins a composition that dropped the argv check."""
    g = _guard()
    argv = list(CLEAN_ARGV) + ["--bare"]
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_oauth_only(dict(PINNED_OAUTH_ENV), argv, list(ISOLATED_PANE_ARGV), dict(CLEAN_SERVER_ENV))


def test_oauth_only_raises_when_pane_not_isolated():
    """assert_oauth_only must COMPOSE assert_pane_env_isolated — a non-env-i pane raises even though
    env/argv/server are clean (pins a composition that forgot the pane-isolation half)."""
    g = _guard()
    non_isolated = list(CLEAN_ARGV)
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_oauth_only(dict(PINNED_OAUTH_ENV), list(CLEAN_ARGV), non_isolated, dict(CLEAN_SERVER_ENV))


def test_oauth_only_raises_when_server_env_leaks_a_key():
    """assert_oauth_only catches a key in the tmux SERVER env (the pane-isolation half covers
    server_env) on otherwise-clean inputs."""
    g = _guard()
    leaky_server = dict(CLEAN_SERVER_ENV, ANTHROPIC_API_KEY="leak")
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_oauth_only(dict(PINNED_OAUTH_ENV), list(CLEAN_ARGV), list(ISOLATED_PANE_ARGV), leaky_server)


# ==================================================================================================
# 4. check_credential_health — the CLAUDE-SPECIFIC positive check (token present + unexpired).
#    auth_expired is a DISTINCT class so a token lapse reads as "refresh the token", NOT a
#    fleet-wide model-outage storm (DAEMON §6.3).
# ==================================================================================================

def test_credential_health_passes_on_pinned_env_with_token():
    """PASSES on the pinned env (CLAUDE_CODE_OAUTH_TOKEN present)."""
    g = _guard()
    assert g.check_credential_health(dict(PINNED_OAUTH_ENV)) is None


def test_credential_health_raises_AuthExpired_on_absent_token():
    """RAISES AuthExpired when CLAUDE_CODE_OAUTH_TOKEN is absent (genesis credential precondition,
    DAEMON §7).

    Mutant pinned: a guard that does NOT raise on an absent token -> caught here.
    """
    g = _guard()
    env = {k: v for k, v in PINNED_OAUTH_ENV.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    with pytest.raises(g.AuthExpired):
        g.check_credential_health(env)


def test_credential_health_absent_token_raises_AuthExpired_NOT_ApiKeyForbidden():
    """The class distinction is LOAD-BEARING: an absent OAuth token raises AuthExpired SPECIFICALLY,
    NOT ApiKeyForbidden — so a token lapse reads as "refresh the token", not a fleet-wide
    model-outage storm (DAEMON §6.3).

    Mutant pinned: raising ApiKeyForbidden (or any non-AuthExpired) on an absent token -> caught.
    We pin it by catching AuthExpired and then asserting the SAME input is NOT caught as
    ApiKeyForbidden (which it would be if the impl wrongly raised the negative-gate class).
    """
    g = _guard()
    env = {k: v for k, v in PINNED_OAUTH_ENV.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}

    # It must raise AuthExpired...
    with pytest.raises(g.AuthExpired):
        g.check_credential_health(env)

    # ...and it must NOT be catchable as ApiKeyForbidden. If the impl raised ApiKeyForbidden, this
    # `pytest.raises(AuthExpired)` would let it propagate and the test would error -> caught. We make
    # the distinction explicit: catching only ApiKeyForbidden must NOT swallow this error.
    if not (issubclass(g.AuthExpired, g.ApiKeyForbidden) or issubclass(g.ApiKeyForbidden, g.AuthExpired)):
        with pytest.raises(g.AuthExpired):
            try:
                g.check_credential_health(env)
            except g.ApiKeyForbidden:  # pragma: no cover - asserts the wrong class is NOT raised
                pytest.fail(
                    "check_credential_health raised ApiKeyForbidden on an absent OAuth token; "
                    "it must raise the DISTINCT AuthExpired class (DAEMON §6.3)"
                )


def test_AuthExpired_and_ApiKeyForbidden_are_DISTINCT_classes():
    """The two exception classes are distinct (neither is the other, and neither subclasses the other)
    — so a credential lapse and a forbidden-api-key path are independently catchable. This is the
    load-bearing class distinction (§2.11 / DAEMON §6.3)."""
    g = _guard()
    assert g.AuthExpired is not g.ApiKeyForbidden
    assert not issubclass(g.AuthExpired, g.ApiKeyForbidden), (
        "AuthExpired must NOT subclass ApiKeyForbidden — a token lapse must not be catchable as a "
        "forbidden-api-key error"
    )
    assert not issubclass(g.ApiKeyForbidden, g.AuthExpired), (
        "ApiKeyForbidden must NOT subclass AuthExpired"
    )
    # Both are real exception types.
    assert issubclass(g.AuthExpired, Exception)
    assert issubclass(g.ApiKeyForbidden, Exception)


def test_AuthExpired_is_a_spawn_failure_distinct_from_model_unavailable():
    """§2.13: AuthExpired subclasses SpawnFailure (a DISTINCT spawn-failure class) so an auth lapse
    is classified separately from model_unavailable (DAEMON §6.3 — not a fleet-wide outage storm).

    We assert AuthExpired is NOT the same class as the generic negative-gate ApiKeyForbidden and that
    an absent-token failure carries the auth_expired semantics (its class name is AuthExpired)."""
    g = _guard()
    env = {k: v for k, v in PINNED_OAUTH_ENV.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    with pytest.raises(g.AuthExpired) as excinfo:
        g.check_credential_health(env)
    raised = excinfo.value
    # It is an AuthExpired, and NOT (mistakenly) an ApiKeyForbidden instance.
    assert isinstance(raised, g.AuthExpired)
    assert not isinstance(raised, g.ApiKeyForbidden), (
        "the absent-token failure must not be an ApiKeyForbidden instance — the class distinction "
        "is what lets the escalation say 'refresh the token' instead of 'model is down'"
    )


def test_credential_health_does_not_reject_clean_env_lacking_api_keys():
    """check_credential_health is the POSITIVE token check ONLY — it must NOT raise merely because the
    env has no api key (that negative concern belongs to assert_no_api_key). A pinned env with the
    token present passes regardless of how minimal it is."""
    g = _guard()
    minimal = {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    assert g.check_credential_health(minimal) is None


# ==================================================================================================
# 5. ANTHROPIC_AUTH_TOKEN — forbidden on EVERY pane (2026-07-16, the L5 Codex-path ruling).
#    The 2026-07-13 proxied-seat carve-out (the Sol/CLIProxyAPI wiring) was removed with the Sol
#    proxy machinery: no seat is proxied anymore, so an auth-bearer var CC honors is a stray
#    credential on ANY pane. The hard invariant (raw ANTHROPIC_API_KEY / OPENAI_API_KEY / --bare)
#    is unchanged and unconditional.
# ==================================================================================================

_STRAY_BEARER_ENV = {
    "ANTHROPIC_AUTH_TOKEN": "stray-auth-bearer-must-not-ride-any-pane",
}


def test_no_api_key_refuses_auth_token_on_every_seat():
    """ANTHROPIC_AUTH_TOKEN present RAISES ApiKeyForbidden unconditionally — there is no
    proxied-seat allowance left (2026-07-16 reversal). Mutant pinned: a guard that re-adds a
    blanket or seat-scoped allowance for the bearer -> caught here."""
    g = _guard()
    env = dict(PINNED_OAUTH_ENV)
    env.update(_STRAY_BEARER_ENV)
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_no_api_key(env, list(CLEAN_ARGV))


def test_assert_oauth_only_refuses_stray_auth_token():
    """The composed gate refuses a pane env carrying the stray bearer while the pane-isolation
    half stays in force (a clean env still passes elsewhere in this suite)."""
    g = _guard()
    env = dict(PINNED_OAUTH_ENV)
    env.update(_STRAY_BEARER_ENV)
    with pytest.raises(g.ApiKeyForbidden):
        g.assert_oauth_only(env, list(CLEAN_ARGV), list(ISOLATED_PANE_ARGV), dict(CLEAN_SERVER_ENV))
