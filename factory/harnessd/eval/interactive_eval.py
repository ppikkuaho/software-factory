"""interactive_eval — drive a REAL jailed agent-under-test through a multi-turn conversation against
a COUNTERPART-SIMULATOR (an LLM playing the human/parent), over interactive tmux.

The instrument for the interactive-level behavioural evals (L1 intake; L2/L3 escalations). The
agent-under-test runs jailed + dialog-free (the deterministic-trust + seatbelt path). The
counterpart-sim is a separate `claude -p` call playing whoever is above/outside the agent (the human
user for L1, the parent coordinator for L2-L5), driven by a SCENARIO brief that controls what it
knows and — for the LEAK TEST — what it WITHHOLDS unless asked.

Turn detection (the linchpin): the agent is WAITING for input iff its tmux pane is STABLE (unchanged
across polls) AND shows an input prompt with no active spinner. This reuses the harness's own
liveness signals (idle-prompt + no-progress, WATCHDOG §3.5). A stable working-spinner = still
working; a never-stabilizing pane past a wall-clock cap = FROZEN (the freeze the interactive
transport makes visible, which `-p` hides).

NO model is mocked — both the agent and the counterpart are real. Usage is bounded by max_turns +
the per-call budget. Run via the eval scripts, not the default test suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import harnessd.spawn.cc_config as cc_config
import harnessd.spawn.sandbox as sandbox


# ---------------------------------------------------------------------------
# Config — the pinned binary + the self-auth jail tier (trusted box; keychain readable so the binary
# self-auths, write-jail + all OTHER secret-denies hold). Secure tier (inject token + keychain closed)
# is gated on the fresh-token-read problem (register).
# ---------------------------------------------------------------------------

def _root() -> Path:
    return Path(__file__).resolve().parents[2]


CC = str(_root() / ".cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
CONFIG_DIR = str(_root() / ".cc-pinned/config")
SYSTEM_PROMPT = str(_root() / "operational/shared/system-prompt.md")


def _self_auth_jail_profile(workroot: str, tmpdir: str) -> str:
    """Render the §2.3 seatbelt profile, SELF-AUTH tier: drop the keychain mach-deny + the
    Library/Keychains read-deny so the binary self-auths (trusted-box residual); everything else
    (write-jail, all other secret-denies) holds. READ_DENY_ROOT empty for a single-node eval."""
    prof = sandbox.render_profile(
        {"WORKROOT": workroot, "TMPDIR": tmpdir, "CONFIG": CONFIG_DIR,
         "HOME": os.path.expanduser("~"), "READ_DENY_ROOT": ""})
    keep = []
    for line in prof.split("\n"):
        if any(x in line for x in ("mach-lookup", "SecurityServer", "securityd", "mach service",
                                   "Library/Keychains")):
            continue
        keep.append(line)
    return "\n".join(keep)


# ---------------------------------------------------------------------------
# tmux helpers (a dedicated -L socket per eval; torn down at the end).
# ---------------------------------------------------------------------------

class Pane:
    def __init__(self, socket: str, session: str = "eval"):
        self.socket = socket
        self.session = session

    def _tmux(self, *args, check=False):
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True, text=True,
                              check=check)

    def new_session(self, command: str, width=200, height=50):
        self._tmux("new-session", "-d", "-s", self.session, "-x", str(width), "-y", str(height),
                   command, check=True)

    def capture(self) -> str:
        return self._tmux("capture-pane", "-p", "-t", self.session).stdout

    def capture_full(self) -> str:
        # full scrollback (history), so the agent's growing output is captured, not just the viewport
        return self._tmux("capture-pane", "-p", "-S", "-", "-t", self.session).stdout

    def send_text(self, text: str):
        # type the text then Enter (two calls — Claude Code's input box submits on Enter)
        self._tmux("send-keys", "-t", self.session, "-l", text)
        time.sleep(0.4)
        self._tmux("send-keys", "-t", self.session, "Enter")

    def send_key(self, key: str):
        self._tmux("send-keys", "-t", self.session, key)

    def is_alive(self) -> bool:
        r = self._tmux("list-panes", "-t", self.session, "-F", "#{pane_dead}")
        return r.returncode == 0 and r.stdout.strip() == "0"

    def kill(self):
        self._tmux("kill-server")


# ---------------------------------------------------------------------------
# Turn detection — is the agent WAITING (our turn), WORKING, or DONE/FROZEN.
# ---------------------------------------------------------------------------

# Markers that the pane is actively WORKING (a spinner / interrupt hint present).
_WORKING_MARKERS = ("esc to interrupt", "Skedaddling", "Thinking", "Pondering", "Cogitating",
                    "almost done", "tokens ·", "↑", "↓ ")
# Markers that the agent is at a question/selection waiting for the human.
_PROMPT_MARKERS = ("Enter to select", "Enter to confirm", "Tab/Arrow", "Type something",
                   "❯ 1.", "1. ")


def classify(prev: str, cur: str) -> str:
    """working | waiting | idle — from two consecutive pane captures.

    working: a spinner/interrupt marker is present (the agent is generating).
    waiting: the pane is STABLE (cur == prev) and shows a prompt/selection (the agent wants input).
    idle:    stable but no clear prompt (ambiguous — treat as waiting after the stability window).
    """
    low = cur.lower()
    if any(m.lower() in low for m in _WORKING_MARKERS):
        return "working"
    if cur.strip() != prev.strip():
        return "working"  # still changing -> streaming
    # stable:
    if any(m in cur for m in _PROMPT_MARKERS):
        return "waiting"
    return "idle"


@dataclass
class Turn:
    speaker: str        # "agent" | "counterpart" | "system"
    text: str


@dataclass
class EvalRun:
    level: str
    scenario: str
    transcript: list = field(default_factory=list)   # list[Turn]
    artifacts: dict = field(default_factory=dict)     # filename -> content
    outcome: str = "incomplete"                       # done | frozen | budget | error
    pane_tail: str = ""

    def add(self, speaker, text):
        self.transcript.append(Turn(speaker, text))


# ---------------------------------------------------------------------------
# The counterpart-simulator — an LLM playing the human/parent, per a scenario brief.
# ---------------------------------------------------------------------------

_COUNTERPART_SYS = (
    "You are role-playing a HUMAN in a software-intake conversation. An intake agent is interviewing "
    "you about something you want built. Stay strictly in character per the SCENARIO. Rules: (1) Answer "
    "the agent's questions as this human would — concise, natural, first person. (2) CRITICAL: only "
    "reveal what the SCENARIO says you know, and WITHHOLD anything the scenario marks as withheld unless "
    "the agent SPECIFICALLY asks for it — never volunteer it. (3) If the agent asks a multiple-choice "
    "question, answer in plain words (the harness maps it). (4) Do not coach the agent or break "
    "character. (5) If the agent has clearly finished intake (reflected back a spec / said it is done), "
    "reply with exactly: [HUMAN-SATISFIED]. Output ONLY the human's next utterance (or that token)."
)


def counterpart_reply(scenario: str, agent_said: str, history: str) -> str:
    """One real `claude -p` call: the human's next utterance given the scenario + the agent's output."""
    prompt = (
        f"SCENARIO (who you are + what you know + what to withhold):\n{scenario}\n\n"
        f"CONVERSATION SO FAR:\n{history}\n\n"
        f"THE AGENT JUST SAID / ASKED:\n{agent_said}\n\n"
        f"Your next utterance as the human (or [HUMAN-SATISFIED] if intake is clearly complete):"
    )
    r = subprocess.run(
        ["env", "-i", f"CLAUDE_CONFIG_DIR={CONFIG_DIR}", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
         "DISABLE_AUTOUPDATER=1", f"HOME={os.path.expanduser('~')}", CC,
         "--append-system-prompt", _COUNTERPART_SYS, "-p", prompt],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    return (r.stdout or "").strip()


# ---------------------------------------------------------------------------
# The eval loop — spawn the jailed agent, drive the multi-turn conversation.
# ---------------------------------------------------------------------------

def _artifacts(workspace: Path, exclude=("brief.md", "BRIEF.md")) -> dict:
    out = {}
    for p in sorted(workspace.rglob("*")):
        if p.is_file() and p.name not in exclude and ".tmp" not in p.parts and p.suffix in (".md", ".json", ".txt", ""):
            try:
                out[str(p.relative_to(workspace))] = p.read_text(encoding="utf-8")[:8000]
            except (OSError, ValueError):
                pass
    return out


def _wait_for_turn(pane: Pane, workspace: Path, *, work_timeout=240, poll=4):
    """Poll until the agent is WAITING (our turn) / DONE (artifact) / FROZEN. Returns the state."""
    prev = ""
    stable_since = None
    t0 = time.time()
    last_change = time.time()
    while True:
        time.sleep(poll)
        if not pane.is_alive():
            return "exited"
        cur = pane.capture()
        # done: an intake artifact appeared
        if _artifacts(workspace):
            return "done"
        state = classify(prev, cur)
        if state == "working":
            last_change = time.time()
            stable_since = None
        elif state == "waiting":
            return "waiting"
        else:  # idle (stable, no clear prompt)
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since > poll * 2:
                return "waiting"  # stable + quiet long enough -> treat as waiting
        prev = cur
        if time.time() - last_change > work_timeout:
            return "frozen"


def _latest_agent_block(cur: str, prev: str) -> str:
    """The agent's new output since the last capture (best-effort: the tail of the pane)."""
    # crude: return the last ~30 lines that aren't the welcome banner / footer
    lines = [l for l in cur.split("\n")
             if l.strip() and not l.strip().startswith("│") and "bypass permissions" not in l
             and "shift+tab" not in l and not set(l.strip()) <= {"─", " "}]
    return "\n".join(lines[-30:])


def run_interactive_eval(level: str, workspace: Path, initial_task: str, scenario: str,
                         *, max_turns=8, boot_wait=12) -> EvalRun:
    """Spawn a jailed agent in `workspace`, drive it against the counterpart-sim per `scenario`."""
    workspace = Path(workspace)
    (workspace / ".tmp").mkdir(parents=True, exist_ok=True)
    run = EvalRun(level=level, scenario=scenario)

    # deterministic trust (no dialogs) + the self-auth jail
    cc_config.seed_trust(CONFIG_DIR, str(workspace))
    prof = workspace / "jail.sb"
    prof.write_text(_self_auth_jail_profile(str(workspace), str(workspace / ".tmp")), encoding="utf-8")

    socket = "eval-" + os.urandom(3).hex()
    pane = Pane(socket)
    cmd = (f"/usr/bin/sandbox-exec -f '{prof}' env -i "
           f"CLAUDE_CONFIG_DIR='{CONFIG_DIR}' CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 "
           f"DISABLE_AUTOUPDATER=1 CLAUDE_CODE_TMPDIR='{workspace}/.tmp' HOME='{os.path.expanduser('~')}' "
           f"sh -c 'cd \"{workspace}\" && exec \"{CC}\" --system-prompt-file \"{SYSTEM_PROMPT}\" "
           f"--add-dir \"{_root()}\" --dangerously-skip-permissions'")
    try:
        pane.new_session(cmd)
        time.sleep(boot_wait)
        run.add("system", f"[boot] pane:\n{pane.capture()[:600]}")

        # initial task
        pane.send_text(initial_task)
        run.add("system", f"[task] {initial_task}")

        history = ""
        for turn_i in range(max_turns):
            state = _wait_for_turn(pane, workspace)
            cur = pane.capture()
            if state in ("done", "exited"):
                run.outcome = "done" if state == "done" else "exited"
                run.add("agent", _latest_agent_block(cur, ""))
                break
            if state == "frozen":
                run.outcome = "frozen"
                run.add("system", "[FROZEN] agent did not progress within the work timeout")
                break
            # waiting: capture the agent's question, get the human's reply
            agent_block = _latest_agent_block(cur, "")
            run.add("agent", agent_block)
            history += f"\nAGENT: {agent_block}\n"
            reply = counterpart_reply(scenario, agent_block, history)
            if "[HUMAN-SATISFIED]" in reply:
                run.add("counterpart", "[HUMAN-SATISFIED] — letting the agent finalize")
                pane.send_text("That's everything from me — please finalize the intent-spec now.")
                # give it a final work window
                final = _wait_for_turn(pane, workspace)
                run.outcome = "done" if _artifacts(workspace) else final
                break
            run.add("counterpart", reply)
            history += f"HUMAN: {reply}\n"
            pane.send_text(reply)
        else:
            run.outcome = "budget"

        run.artifacts = _artifacts(workspace)
        run.pane_tail = pane.capture()
    finally:
        pane.kill()
    return run


# ---------------------------------------------------------------------------
# Autonomous level eval (L2-L5) — synthetic upstream input, the agent works to an output artifact.
# The level mostly works AUTONOMOUSLY (reads its input → produces its output); the counterpart-sim is
# used ONLY if the agent ESCALATES (asks a question). Far simpler than the L1 interactive intake.
# ---------------------------------------------------------------------------

def generate_synthetic(spec_prompt: str, *, timeout=180) -> str:
    """One-shot LLM generation of a synthetic upstream artifact (e.g. a contract-valid intent-spec).

    The cheap way to make a faithful upstream INPUT without running the full upstream cascade (the
    user's "create a synthetic one"): hand the model the output contract + the request, get the artifact.
    Unjailed (it is test-harness scaffolding, not an agent-under-test), no system prompt.
    """
    r = subprocess.run(
        ["env", "-i", f"CLAUDE_CONFIG_DIR={CONFIG_DIR}", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
         "DISABLE_AUTOUPDATER=1", f"HOME={os.path.expanduser('~')}", CC, "-p", spec_prompt],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    return (r.stdout or "").strip()


def run_autonomous_eval(level: str, workspace: Path, initial_task: str, escalation_scenario: str = "",
                        *, work_timeout=900, **_ignored) -> EvalRun:
    """Spawn a jailed level agent in PRINT mode that reads its synthetic input + works to completion,
    producing its output artifact.

    An autonomous artifact-producing level (L2-L5) does NOT need an interactive loop — it reads its
    input, does its work, writes its output artifact, and exits. So we run `claude -p` (jailed +
    dialog-free via seed_trust + skip-permissions, so the freeze that broke -p before is gone) with a
    WALL-CLOCK TIMEOUT as the freeze guard: a frozen -p never returns, so a timeout detects it (the
    coarser-but-real freeze detection the user's concern asked for). The level records open decisions as
    deferred ADRs (per its task) rather than blocking — so it does not need the counterpart mid-run; the
    counterpart-sim is reserved for the genuinely-interactive L1 intake + the cascade-dynamics escalation
    round-trips. The leak test is judged from the PRODUCED ARTIFACT (did it ADR-defer or invent?).
    """
    workspace = Path(workspace)
    (workspace / ".tmp").mkdir(parents=True, exist_ok=True)
    run = EvalRun(level=level, scenario=escalation_scenario)

    cc_config.seed_trust(CONFIG_DIR, str(workspace))
    prof = workspace / "jail.sb"
    prof.write_text(_self_auth_jail_profile(str(workspace), str(workspace / ".tmp")), encoding="utf-8")
    seed_files = {p.name for p in workspace.iterdir() if p.is_file()}

    cmd = ["/usr/bin/sandbox-exec", "-f", str(prof), "env", "-i",
           f"CLAUDE_CONFIG_DIR={CONFIG_DIR}", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
           "DISABLE_AUTOUPDATER=1", f"CLAUDE_CODE_TMPDIR={workspace}/.tmp",
           f"HOME={os.path.expanduser('~')}", "sh", "-c",
           f"cd '{workspace}' && exec '{CC}' --system-prompt-file '{SYSTEM_PROMPT}' "
           f"--add-dir '{_root()}' --dangerously-skip-permissions -p {_shquote(initial_task)}"]
    run.add("system", f"[task] {initial_task}")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=work_timeout)
        run.add("agent", (r.stdout or "")[-6000:])
        run.pane_tail = (r.stderr or "")[-2000:]
        run.outcome = "done" if r.returncode == 0 else f"exit_{r.returncode}"
    except subprocess.TimeoutExpired as exc:
        run.outcome = "frozen"
        run.add("system", f"[FROZEN] -p did not return within {work_timeout}s (the freeze guard fired)")
        run.pane_tail = (exc.stdout or b"")[-2000:].decode("utf-8", "replace") if exc.stdout else ""
    run.artifacts = {k: v for k, v in _artifacts(workspace, exclude=tuple(seed_files) + ("jail.sb",)).items()}
    if run.artifacts and run.outcome.startswith("exit"):
        run.outcome = "done"  # produced its artifact even if the process exit code was nonzero
    print(f"[autonomous {level}] returned in {time.time()-t0:.0f}s outcome={run.outcome} "
          f"artifacts={list(run.artifacts)}")
    return run


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def run_jailed(workspace: Path, task: str, *, work_timeout=600) -> tuple:
    """Run ONE jailed -p agent in `workspace` with `task`; return (stdout, returncode|'timeout').

    The reusable primitive under run_autonomous_eval — used by the execute-review pair (L5 then L5+)
    so each agent runs jailed + dialog-free in the SAME workspace (L5+ must see L5's code).
    """
    workspace = Path(workspace)
    (workspace / ".tmp").mkdir(parents=True, exist_ok=True)
    cc_config.seed_trust(CONFIG_DIR, str(workspace))
    prof = workspace / "jail.sb"
    prof.write_text(_self_auth_jail_profile(str(workspace), str(workspace / ".tmp")), encoding="utf-8")
    cmd = ["/usr/bin/sandbox-exec", "-f", str(prof), "env", "-i",
           f"CLAUDE_CONFIG_DIR={CONFIG_DIR}", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
           "DISABLE_AUTOUPDATER=1", f"CLAUDE_CODE_TMPDIR={workspace}/.tmp",
           f"HOME={os.path.expanduser('~')}",
           # tool-cache redirect so a real pip/npm install lands inside the jail (§2.3)
           *[f"{k}={v}" for k, v in sandbox.cache_redirect_env(str(workspace)).items()],
           "sh", "-c",
           f"cd '{workspace}' && exec '{CC}' --system-prompt-file '{SYSTEM_PROMPT}' "
           f"--add-dir '{_root()}' --dangerously-skip-permissions -p {_shquote(task)}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=work_timeout)
        return (r.stdout or "")[-8000:], r.returncode
    except subprocess.TimeoutExpired:
        return "[FROZEN — no return within timeout]", "timeout"
