"""Increment 16 — the ONE real in-role boot (the subscription gate).

@pytest.mark.real_boot — DESELECTED by default (the only test that spends the model subscription).
Run explicitly with:  python3 -m pytest -m real_boot tests/test_real_boot.py -s

It drives the REAL pinned Claude Code binary (.cc-pinned, v2.1.152) with the REAL OAuth token and the
current harness boot recipe: `env -i` from-empty isolation plus `--system-prompt-file` pointed at the
per-spawn composed identity bundle (`.identity-prompt.md`: shared prompt first, then the level's
identity docs). It asserts the model boots IN-ROLE rather than as the default coding assistant,
recording the model_used fact. This is the last mock removed — the real model behind the pane.

PRECONDITION HANDLING (an honest gate, not a vacuous pass):
  * no token file / empty            -> SKIP (the pinned install is not authed)
  * token present but auth 401s      -> SKIP with "refresh the token" (the FORK-TOKEN-EXPIRY reality:
                                        `test-ant-oat01` tokens expire; check_credential_health is
                                        presence-only in v1, so an expired token is an ENVIRONMENT
                                        precondition, not a logic bug — surfaced loud, skipped clean)
  * token valid                      -> RUN the real in-role boot and ASSERT the framing took.
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.commissioning as commissioning
import harnessd.config as config
import harnessd.daemon as daemon
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.spawn.adapters.claude_code as claude_code
import harnessd.turn_state as turn_state
from harnessd.spawn import chokepoint, oauth_guard, tmux
from harnessd.spawn import blinders

pytestmark = pytest.mark.real_boot

_ROOT = Path(__file__).resolve().parents[1]
_CC = _ROOT / ".cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
_CONFIG_DIR = _ROOT / ".cc-pinned/config"
_TOKEN_FILE = _CONFIG_DIR / ".oauth_token"
_SYSTEM_PROMPT = _ROOT / config.SYSTEM_PROMPT_FILE


def _identity_prompt(level: str, workspace: Path) -> Path:
    path, _sha = claude_code._compose_identity_prompt(
        config.get_level_config(level),
        str(workspace),
    )
    return Path(path)


def _pane_env(token: str) -> dict:
    """The exact commissioning isolation env before containment adds cache/temp vars."""
    return {
        "CLAUDE_CONFIG_DIR": str(_CONFIG_DIR),
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "PATH": commissioning.PRODUCTION_PANE_PATH,
    }


def _run_pinned(
    token: str,
    prompt: str,
    *,
    system_prompt_file: Path | None = None,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run the pinned binary in print mode through the REAL `env -i` from-empty isolation.

    `env -i <4 vars> claude --system-prompt-file <composed-identity> -p <prompt>` is the harness
    pane vector (minus the detached-tmux wrapper and tmux cwd wrapper) — the same argv/env shape the
    adapter builds after composing identity.
    """
    argv = [
        "env", "-i",
        *[f"{k}={v}" for k, v in _pane_env(token).items()],
        str(_CC),
        "--system-prompt-file", str(system_prompt_file or _SYSTEM_PROMPT),
        "-p", prompt,
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)


@pytest.fixture(scope="module")
def raw_real_token():
    if not _CC.exists():
        pytest.skip(f"pinned binary not installed at {_CC}")
    if not _SYSTEM_PROMPT.exists():
        pytest.skip(f"shared system prompt not found at {_SYSTEM_PROMPT}")
    if not _TOKEN_FILE.exists():
        pytest.skip("pinned install has no OAuth token (.cc-pinned/config/.oauth_token absent)")
    token = _TOKEN_FILE.read_text().strip()
    if not token:
        pytest.skip("pinned OAuth token file is empty")
    return token


@pytest.fixture(scope="module")
def real_token(raw_real_token):
    # Cheap real auth-probe (one minimal turn). A 401 here = expired/invalid token -> SKIP (an
    # environment precondition: FORK-TOKEN-EXPIRY — refresh the pinned install's token), not a fail.
    probe = _run_pinned(raw_real_token, "Reply with exactly: OK", timeout=60)
    out = (probe.stdout + probe.stderr)
    if "401" in out or "Invalid authentication" in out or "Failed to authenticate" in out:
        pytest.skip(
            "pinned OAuth token is expired/invalid (401) — refresh it (test-ant-oat01 tokens expire; "
            "v1 check_credential_health is presence-only, FORK-TOKEN-EXPIRY). Re-auth the pinned "
            "install, then re-run `pytest -m real_boot`."
        )
    return raw_real_token


def test_real_in_role_boot(real_token, tmp_path):
    """The pinned binary, booted with the composed identity prompt via the real env-i isolation,
    responds IN-ROLE as the requested harness seat — NOT as the default coding assistant. This is
    the one real proof that the LR-4 identity-autoload recipe works end-to-end with the real model +
    real token, and that model_used is the recorded fact."""
    identity = _identity_prompt("L1", tmp_path)
    result = _run_pinned(
        real_token,
        "Without using tools or creating files, answer in one sentence: "
        "what is your role here, and what should you do first?",
        system_prompt_file=identity,
        cwd=tmp_path,
    )
    assert result.returncode == 0, f"real boot failed (rc={result.returncode}): {result.stderr[:400]}"
    reply = (result.stdout or "").lower()
    assert reply.strip(), "the real boot produced no output"
    # IN-ROLE: the composed prompt carries the L1 identity docs. A booted-in-role agent reflects the
    # System Orchestrator/L1 posture and its document-first operating surface, not the default
    # coding-assistant framing.
    in_role_markers = ("system orchestrator", "l1", "client intent", "portfolio", "read")
    default_assistant_markers = ("help you with your code", "coding assistant", "how can i help")
    assert any(m in reply for m in in_role_markers), (
        f"the boot does not read as in-role (the harness framing did not take): {reply[:300]!r}"
    )
    assert not any(m in reply for m in default_assistant_markers), (
        f"the boot read as the DEFAULT coding assistant — the --system-prompt-file did not replace "
        f"base block 2: {reply[:300]!r}"
    )
    # model_used is the harness FACT: the pinned binary is the configured Claude Code seat.
    assert config.PINNED_BINARY_VERSION == "2.1.152"


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _adopted_failed_tool_rows(rows: list[dict], *, tool_name: str) -> list[dict]:
    """Select truthful adopted failure edges for the intended tool from the layered event log."""
    return [
        row
        for row in rows
        if row.get("row_kind") == turn_state.ADOPTED_HOOK_EVENT
        and row.get("hook_event") == "PostToolUseFailure"
        and (row.get("detail") or {}).get("tool_name") == tool_name
    ]


def _wait_for(predicate, *, timeout: float = 180.0, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def test_production_shaped_opus5_boot(raw_real_token, tmp_path):
    """One real chokepoint boot proves the retained pin's complete Opus 5 operator surface."""
    if shutil.which("tmux") is None:
        pytest.skip("production-shaped real boot requires tmux")

    node_address = "L1#exec"
    node_dir = addressing.node_dir(node_address, tmp_path)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "brief.md").write_text(
        "# Q7 production-shaped boot\n\n"
        "This is a single-turn substrate proof, not product work. Do not spawn children. "
        "Use Bash exactly once with `printf 'JAILED_BASH_OK\\n' > .q7-jailed-bash-marker; exit 7`. "
        "That nonzero result is intentional. Then use non-Bash tools to read "
        "`.sign-off.exec.json` and write the required DONE terminal signal with its current owner "
        "token. End with exactly "
        "`Q7_OPUS5_PRODUCTION_BOOT`. Do not change repository files.\n",
        encoding="utf-8",
    )
    (node_dir / "plan.md").write_text(
        "# Plan\n\n- [x] Run the bounded production-shaped boot proof.\n",
        encoding="utf-8",
    )
    (node_dir / "report.md").write_text(
        "# Q7 Boot Evidence\n\nQ7_OPUS5_PRODUCTION_BOOT\n",
        encoding="utf-8",
    )

    seed_session = str(uuid.uuid4())
    seed_token = fencing.mint_owner_token(node_address, "q7-probe", seed_session, 1)
    binding = {
        "node_address": node_address,
        "parent_address": None,
        "level": "L1",
        "subagent_id": "q7-probe",
        "session_uuid": seed_session,
        "state": "planned",
        "generation": 0,
        "lease_epoch": 1,
        "owner_token": seed_token,
        "last_applied_seq": 0,
        "spec_pointer": str(node_dir / "brief.md"),
        "liveness_state": "claimed",
        "gate_crossed_at": None,
        "paused_at": None,
        "workspace": str(node_dir),
        "tmux_target": addressing.session_name_for(node_address),
    }

    socket = "q7-opus5-" + uuid.uuid4().hex[:12]
    previous_root = ledger.RUNTIME_ROOT
    previous_socket = getattr(tmux, "_SOCKET", None)
    previous_adapter = chokepoint.ADAPTER
    previous_spawn_env = chokepoint.SPAWN_ENV
    target = None
    listener = None
    try:
        ledger.RUNTIME_ROOT = tmp_path
        ledger.write_binding({node_address: binding}, _lock_held=True)
        listener = daemon.make_ipc_listener(tmp_path)
        __import__("threading").Thread(
            target=ipc.serve_forever,
            args=(listener,),
            name="real-boot-hook-adoption",
            daemon=True,
        ).start()
        tmux.set_socket(socket)
        # Test-only evidence retention: keep an exited pane and its final output. The short-lived
        # anchor starts the dedicated server so the global window option exists before the actor
        # can exit; it is removed immediately after the production pane opens.
        anchor = "real-boot-diagnostic-anchor"
        subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "new-session",
                "-d",
                "-s",
                anchor,
                "/usr/bin/tail",
                "-f",
                "/dev/null",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["tmux", "-L", socket, "set-option", "-gw", "remain-on-exit", "on"],
            check=True,
            capture_output=True,
            text=True,
        )
        adapter = claude_code.ClaudeCodeAdapter(tmux=tmux)
        chokepoint.set_adapter(adapter)
        pane_env = _pane_env(raw_real_token)
        chokepoint.set_spawn_env(pane_env)
        level_config = replace(
            config.LEVEL_CONFIGS["L1"],
            unjailed_skip_permissions=False,
            blinders_mode=blinders.ENFORCE,
        )

        result = chokepoint.claim_and_spawn(
            node_address,
            expected_state="planned",
            expected_generation=0,
            expected_owner_token=seed_token,
            level_config=level_config,
        )
        assert result.ok, f"production-shaped spawn failed: {result!r}"
        target = result.tmux_target
        subprocess.run(
            ["tmux", "-L", socket, "kill-session", "-t", anchor],
            check=False,
            capture_output=True,
            text=True,
        )

        # The real adapter reached its OAuth-only gate before actor open, and this proof uses
        # commissioning's actual PATH before containment prepends Xcode's git directory.
        assert {
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
            "DISABLE_AUTOUPDATER",
        } <= set(result.env)
        assert result.env["CLAUDE_CODE_OAUTH_TOKEN"] == pane_env[
            "CLAUDE_CODE_OAUTH_TOKEN"
        ]
        assert result.env["CLAUDE_CONFIG_DIR"] == pane_env["CLAUDE_CONFIG_DIR"]
        expected_path = commissioning.PRODUCTION_PANE_PATH
        xcode_bin = "/Applications/Xcode.app/Contents/Developer/usr/bin"
        if os.path.isdir(xcode_bin):
            expected_path = f"{xcode_bin}:{expected_path}"
        assert result.env["PATH"] == expected_path
        assert result.env["CLAUDE_CODE_TMPDIR"].startswith(str(node_dir))
        assert result.permission_posture == "jailed-skip-permissions"
        assert "ANTHROPIC_API_KEY" not in result.env
        assert "OPENAI_API_KEY" not in result.env
        oauth_guard.assert_no_api_key(result.env, list(result.argv))

        # The exact model and explicit per-seat settings rode the production argv.
        argv = list(result.argv)
        assert argv[argv.index("--model") + 1] == "claude-opus-5"
        settings_path = Path(argv[argv.index("--settings") + 1])
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert set(settings["hooks"]) == {
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
        }
        assert settings_path.stat().st_mode & 0o777 == 0o400
        assert result.containment_posture["mode"] == blinders.ENFORCE
        assert result.containment_posture["runtime_inner_allows"] == [
            str(_CONFIG_DIR / "session-env" / result.session_uuid)
        ]

        live = ledger.read_binding(node_address)
        handshake_path = addressing.signoff_path(node_address, tmp_path)
        handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
        assert handshake["owner_token"] == live["owner_token"]
        assert handshake["signal_path"] == str(addressing.signal_path(node_address, tmp_path))
        assert result.model_used == "opus-5.0 / claude-code"
        assert result.role_variant == "L1"
        assert Path(result.system_prompt_file).is_file()

        events_path = addressing.turn_events_path(node_address, tmp_path)

        def completed_events():
            rows = _read_jsonl(events_path)
            names = {row.get("hook_event") for row in rows}
            states = {row.get("state") for row in rows}
            if (
                {
                    "UserPromptSubmit",
                    "PreToolUse",
                    "PostToolUse",
                    "PostToolUseFailure",
                    "Stop",
                }
                <= names
                and {
                    turn_state.TURN_RUNNING,
                    turn_state.TOOL_IN_FLIGHT,
                    turn_state.TURN_ENDED,
                }
                <= states
            ):
                return {"rows": rows, "dead": False}
            pane = tmux.list_targets().get(target)
            if pane and int(pane.get("pane_dead") or 0) == 1:
                return {
                    "rows": rows,
                    "dead": True,
                    "pane": tmux.capture_pane(target),
                    "pane_context": pane,
                }
            return None

        event_outcome = _wait_for(completed_events)
        assert event_outcome and not event_outcome.get("dead"), (
            "real hooks did not durably record running/tool/ended edges; "
            f"outcome={event_outcome!r}; rows={_read_jsonl(events_path)!r}; "
            f"pane={tmux.capture_pane(target)!r}; targets={tmux.list_targets()!r}"
        )
        event_rows = event_outcome["rows"]
        failure_rows = _adopted_failed_tool_rows(event_rows, tool_name="Bash")
        assert len(failure_rows) == 1
        failed_id = failure_rows[0]["detail"]["tool_use_id"]
        assert failed_id not in failure_rows[0]["detail"]["in_flight_tools"]
        assert (node_dir / ".q7-jailed-bash-marker").read_text(
            encoding="utf-8"
        ) == "JAILED_BASH_OK\n"

        transcript_path = Path(result.transcript_path)

        def attributed_transcript():
            rows = _read_jsonl(transcript_path)
            attributed = [
                row
                for row in rows
                if row.get("type") == "assistant"
                and (row.get("message") or {}).get("model") == "claude-opus-5"
            ]
            text = json.dumps(attributed, ensure_ascii=True)
            return (rows, attributed) if "Q7_OPUS5_PRODUCTION_BOOT" in text else None

        transcript = _wait_for(attributed_transcript)
        assert transcript, (
            "interactive transcript never attributed an assistant response to claude-opus-5; "
            f"rows={_read_jsonl(transcript_path)!r}"
        )
        transcript_rows, attributed_rows = transcript
        transcript_text = json.dumps(attributed_rows, ensure_ascii=True)
        assert "Q7_OPUS5_PRODUCTION_BOOT" in transcript_text

        state = json.loads(
            addressing.turn_state_path(node_address, tmp_path).read_text(encoding="utf-8")
        )
        assert state["state"] == turn_state.TURN_ENDED
        assert state["last_hook_event"] == "Stop"
        assert state.get("hook_fault") is None

        # The daemon consumes the same durable state and records it on the live binding.
        daemon._adopt_turn_state_for_seat(
            executor,
            tmux,
            node_address,
            ledger.read_binding(node_address),
        )
        adopted = ledger.read_binding(node_address)
        assert adopted["turn_hook_health"] == "healthy"
        assert adopted["turn_state"] == turn_state.TURN_ENDED
        assert adopted["turn_state_hook_event"] == "Stop"
        assert addressing.signal_path(node_address, tmp_path).is_file()
        assert transcript_rows
    finally:
        if listener is not None:
            listener.close()
        if target:
            tmux.kill(target)
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )
        chokepoint.set_adapter(previous_adapter)
        chokepoint.set_spawn_env(previous_spawn_env)
        tmux.set_socket(previous_socket)
        ledger.RUNTIME_ROOT = previous_root
