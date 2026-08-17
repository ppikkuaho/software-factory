"""detector_signals — the RAW signal readers the liveness verdict fuses (Increment 6).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.8 (the FROZEN detector_signals.py interface — exact
    signatures below): jsonl_progress / pane_alive / pane_pid_cpu / read_terminal_signal.
  - IMPLEMENTATION-PLAN §4.1 "terminal-signal reader (the PRODUCER for INCLUDE-item #3,
    fully mockable)" (L564-571) — the stale-owner_token fence is the load-bearing case.
  - Runtime tree layout (§3 tree, L454-471): the nested per-seat `<node-dir>/.signal.<seat>.json`
    (addressing.signal_path) carries {signal: DONE|FAILED|ESCALATED, ts, owner_token, evidence},
    agent-written atomic tmp+rename — the owner_token copied VERBATIM from the chokepoint-seeded
    `<node-dir>/.sign-off.<seat>.json` handshake (addressing.signoff_path, F19: the agent-side
    token source; the brief/env never carry the token).

THE TMUX SEAM (§2.11, frozen Increment 0; concrete tmux.py lands Increment 9):
    pane_alive() reaches tmux ONLY through the module-level `_tmux` reference (the §2.11
    `display-message '#{pane_dead} #{pane_pid}'` interface). In Increment 6 there is no
    concrete tmux.py, so `_tmux` is None and pane_alive's tmux branch is unreachable under
    the test harness — tests MONKEYPATCH `pane_alive` (and the detector's reference to it)
    directly, per the frozen test's `_patch_signals_everywhere`. The seam is a module-level
    name so Increment 9 can bind `_tmux = harnessd.spawn.tmux` (or the detector can inject a
    fake) WITHOUT editing this module's verdict-feeding readers. See escalations/notes in the
    Increment-6 builder report for how this was wired.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from .runtime_failures import runtime_failure_from_transcript_event


# ---------------------------------------------------------------------------
# The TMUX seam (§2.11). MOCKABLE module-level reference. None until Increment 9
# binds the concrete `harnessd/spawn/tmux.py`. pane_alive() calls through this; a
# test that wants real-tmux behavior monkeypatches pane_alive itself (Inc 6 floor).
# ---------------------------------------------------------------------------

_tmux = None  # Increment 9: `import harnessd.spawn.tmux as _tmux` (the §2.11 interface).


# ---------------------------------------------------------------------------
# Fail-loud sentinel for the spawn<->detector contract violation (no transcript_path).
# A binding with no transcript_path is a contract violation that must surface, NEVER
# be silently swallowed into a benign (False, None) 'flat' the verdict reads as idle/dead.
# ---------------------------------------------------------------------------

class MissingTranscriptPath(ValueError):
    """A node/binding carries no transcript_path — the spawn<->detector contract is broken.

    Raised by jsonl_progress (and surfaced by detector.liveness) so the violation fails LOUD
    at the cheapest place to catch it, rather than collapsing the node to a phantom dead/idle.
    """


# The internal size/mtime cache backing jsonl_progress's grew-vs-flat comparison.
# Keyed by node_address. This is the impl's private business (the frozen contract is the
# SIGNAL the verdict fuses, not this cache); tests pin the signal by monkeypatching the
# reader at the detector's call site, so this cache is exercised only by the signal-layer
# tests that drive a real file.
_size_cache: dict[str, int] = {}


def _node_address(node) -> str:
    """The node's stable address (the cache key). Accepts a binding/node-shaped dict."""
    return node["node_address"]


def _transcript_path(node):
    """Resolve the node's transcript_path; a missing/None value is a contract violation.

    Both an explicit ``transcript_path: None`` and an entirely MISSING key are the same
    spawn<->detector violation, so both raise MissingTranscriptPath (fail-loud), never a
    silent benign return.
    """
    path = node.get("transcript_path")
    if not path:
        raise MissingTranscriptPath(
            f"node {node.get('node_address')!r} has no transcript_path — the spawn<->detector "
            "contract requires one; refusing to silently read as flat/dead/idle"
        )
    return path


def _iso_from_mtime(st_mtime: float) -> str:
    """Render a stat st_mtime (epoch seconds) as a tz-aware UTC ISO-8601 string."""
    return datetime.fromtimestamp(st_mtime, tz=timezone.utc).isoformat()


def jsonl_progress(node) -> tuple[bool, str | None]:
    """(grew, mtime_iso) from os.stat(transcript) st_size/st_mtime vs a cached prior (§2.8).

    grew is True iff the transcript's byte size is STRICTLY GREATER than the size cached from
    the prior read for this node — the forward-progress signal the verdict fuses. The first
    read establishes the baseline (grew=False, no prior to grow past); subsequent reads compare
    against the last observed size. mtime_iso is the file's st_mtime rendered as a tz-aware UTC
    ISO-8601 string (the detector compares it via the canonical clock).

    FAIL-LOUD: no transcript_path -> raises MissingTranscriptPath (NEVER a silent (False, None)
    that the verdict would read as a benign flat). An absent FILE (path set but not yet created)
    is a transient pre-write condition, not a contract violation: it reads (False, None).
    """
    address = _node_address(node)
    path = _transcript_path(node)  # raises MissingTranscriptPath on the contract violation

    try:
        st = os.stat(path)
    except FileNotFoundError:
        # The path is contracted but the file is not on disk yet (a just-spawned actor that
        # has not written its first transcript line). This is transient, NOT the contract
        # violation — read as flat with no mtime, do not poison the cache.
        return False, None

    prior = _size_cache.get(address)
    _size_cache[address] = st.st_size
    grew = prior is not None and st.st_size > prior
    return grew, _iso_from_mtime(st.st_mtime)


def runtime_failure_from_transcript(node) -> dict | None:
    """Return a typed runtime failure found in the transcript, if one is present.

    This is deliberately a passive reader: it parses the already-persisted JSONL
    transcript and does not mutate the runtime tree. It exists for runtime-auth
    failures that surface after a pane was successfully spawned. Without this
    hook, a Codex transcript that is explicitly reporting an OAuth refresh
    failure eventually degrades to the generic idle-ladder
    ``watchdog_nonresponse`` class.
    """
    try:
        path = _transcript_path(node)
    except MissingTranscriptPath:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                failure = _runtime_failure_from_transcript_row(row)
                if failure:
                    failure.setdefault("line", line_no)
                    return failure
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return None


def _runtime_failure_from_transcript_row(row: dict) -> dict | None:
    return runtime_failure_from_transcript_event(row)


def pane_alive(node) -> tuple[bool, int | None]:
    """(alive, pane_pid) via the tmux display-message '#{pane_dead} #{pane_pid}' interface (§2.8/§2.11).

    Reaches tmux ONLY through the module-level `_tmux` seam (frozen §2.11 interface; concrete
    tmux.py lands Increment 9). alive is True iff the pane exists and is not pane_dead. In
    Increment 6 `_tmux` is None (no concrete adapter), so this raises if called un-mocked —
    the floor's tests MONKEYPATCH this reader (the boundary the detector calls), per
    §4 subscription-safety (ZERO model usage, tmux mocked).
    """
    if _tmux is None:
        raise RuntimeError(
            "pane_alive: the tmux seam is not bound (Increment 9 binds harnessd.spawn.tmux). "
            "Increment 6 mocks this reader directly — see detector_signals._tmux."
        )
    target = node["tmux_target"]
    # §2.11 interface: list_targets() -> {tmux_target: {pane_pid, pane_dead, window_activity}}.
    targets = _tmux.list_targets()
    info = targets.get(target)
    if info is None:
        return False, None  # pane gone entirely -> not alive
    pane_dead = bool(info.get("pane_dead"))
    pane_pid = info.get("pane_pid")
    return (not pane_dead), (None if pane_dead else pane_pid)


# The CC mid-turn footer marker — present while CC is working a turn (INCLUDING while a Task
# subagent runs under it, when the MAIN transcript sits flat — LR-17) and absent at true idle.
# Single source: watchdog._WORKING_MARKER aliases this constant (the de-drift rule).
PANE_WORKING_MARKER: str = "esc to interrupt"


def pane_activity(node) -> bool:
    """LR-17 — True iff the pane's CAPTURE shows the mid-turn working marker.

    The Run-2 false-idle: L1 dispatched its intake grilling SESSION (a Task subagent — exactly
    what its role doc prescribes) and the MAIN transcript sat flat while the subagent worked;
    the flat-beyond-W rule read it idle and the ladder killed a healthy, spec-conformant agent.
    The pane itself knows better: CC renders 'esc to interrupt' whenever a turn (or subagent)
    is in flight, and drops it at true idle. Best-effort: an unbound seam / capture error
    returns False (never masks a genuinely dead pane — pane_alive already won by then)."""
    if _tmux is None:
        return False
    capture = getattr(_tmux, "capture_pane", None)
    if capture is None:
        return False
    try:
        text = capture(node["tmux_target"]) or ""
    except Exception:  # noqa: BLE001 — a capture hiccup must not invent a verdict
        return False
    return PANE_WORKING_MARKER in text


def pane_pid_cpu(node, pane_pid) -> float | None:
    """Return summed CPU for proper descendants of ``pane_pid`` from one ``/bin/ps`` snapshot.

    ``0.0`` is a known idle descendant tree; ``None`` is UNKNOWN (missing pane pid, process
    disappeared between tmux and ps, malformed output, or probe failure).  The pane process
    itself is excluded: the ruled work signal is a child tool/runtime process consuming CPU,
    not the long-lived TUI shell merely existing.
    """
    try:
        root_pid = int(pane_pid)
    except (TypeError, ValueError):
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,%cpu="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    parents: dict[int, list[int]] = {}
    cpu_by_pid: dict[int, float] = {}
    for raw in result.stdout.splitlines():
        parts = raw.split()
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            cpu = float(parts[2])
        except (TypeError, ValueError):
            continue
        parents.setdefault(ppid, []).append(pid)
        cpu_by_pid[pid] = cpu
    if root_pid not in cpu_by_pid:
        return None

    descendants: list[int] = []
    stack = list(parents.get(root_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        stack.extend(parents.get(pid, []))
    return sum(cpu_by_pid.get(pid, 0.0) for pid in descendants)


# ---------------------------------------------------------------------------
# read_terminal_signal — the FENCED signal-artifact reader (the §4.1 producer for
# INCLUDE-item #3). The nested per-seat `<node-dir>/.signal.<seat>.json`
# (addressing.signal_path) -> {signal, ts, owner_token, evidence}; the agent sources
# its owner_token from the chokepoint-seeded `.sign-off.<seat>.json` handshake in the
# same node dir (F19).
# ---------------------------------------------------------------------------

def _signal_path(node, *, runtime_root=None):
    """The per-SEAT sign-off signal: ``<nested-node-dir>/.signal.<seat>.json`` (``addressing.signal_path``).

    NESTED by path + seat-qualified: exec and review share the node dir (two actors, one node) so the
    signal filename carries the seat to keep their sign-offs distinct. Resolved against the ledger's
    injectable RUNTIME_ROOT (the daemon binds it once; tests bind tmp_path) so the agent-writer and the
    detector-reader agree on the same path without a second seat.
    """
    from . import addressing, ledger  # local import: share the ONE injectable RUNTIME_ROOT seat

    root = runtime_root if runtime_root is not None else ledger.RUNTIME_ROOT
    if root is None:
        raise RuntimeError(
            "read_terminal_signal: ledger.RUNTIME_ROOT is not bound — the runtime tree root is "
            "where nodes/<nested-path>/.signal.<seat>.json lives; bind it (daemon startup / tests)."
        )
    return addressing.signal_path(_node_address(node), root)


def _ts_is_newer(candidate, baseline) -> bool:
    """True iff ``candidate`` is a STRICTLY later instant than ``baseline`` (SM-4 freshness).

    Routes through the canonical clock parser (offset-invariant); tolerant of agent-written
    timestamps — anything unparseable/missing compares NOT-newer (the idempotent default: an
    unprovable freshness claim never re-journals or re-arms anything).
    """
    if not candidate or not baseline:
        return False
    from . import clock  # local import: keep this module import-light

    try:
        return clock.parse_iso(candidate) > clock.parse_iso(baseline)
    except (ValueError, TypeError):
        return False


def escalation_answered(binding, signal_artifact_seen_at=None) -> bool:
    """True iff the node's held ESCALATED slot carries a human answer FRESHER than the question (SM-4).

    The ESCALATED stamp is deliberately never cleared in v1 (the answer RIDES
    terminal_signal=ESCALATED + terminal_note; clearing belongs to the round-trip completion) —
    but a stamp nothing expires made the slot-hold PERMANENT: the detector read legit-waiting
    forever, the idle->prod->FAILED ladder was dead for any node that ever escalated, and a
    second escalation could never journal. This predicate is the EXPIRY: the answer
    (``answered_at``, stamped by executor.post_answer) outranks the question iff it is fresher
    than the binding stamp (``terminal_signal_at``) and the currently read on-disk artifact is the
    same artifact already journaled in ``signal_artifact_seen_at``. Agent-authored artifact clocks
    are deliberately not trusted for idempotency or expiry.
    """
    # 3a canonical answer path: the read-side ESCALATED shim mints a deterministic question id
    # from this signal identity. An ordinary answer message closes that sender-owned row atomically;
    # the legacy answer stamps below remain read compatibility for pre-3a/human-control calls.
    if signal_artifact_seen_at:
        from . import messages

        question = (binding.get("messages") or {}).get(
            messages.escalation_message_id(signal_artifact_seen_at)
        )
        if isinstance(question, dict) and question.get("question_state") == "answered":
            return True

    answered_at = binding.get("answered_at")
    if not answered_at:
        return False  # no answer posted -> the slot-hold stands
    if (
        signal_artifact_seen_at
        and binding.get("signal_artifact_seen_at") != signal_artifact_seen_at
    ):
        return False  # a fresh artifact not yet journaled -> a NEW question is pending
    if _ts_is_newer(binding.get("terminal_signal_at"), answered_at):
        return False  # re-escalated (journaled) AFTER the answer -> waiting again
    return True


def _signal_artifact_identity(path, raw: bytes) -> str:
    """Harness-owned identity for journal-once signal artifacts.

    The artifact payload's ``ts`` is agent-authored and can be skewed or malicious. The durable
    guard therefore keys on facts the harness observes: content hash plus the filesystem's write
    identity. A byte-for-byte rewrite still counts as a new artifact when mtime changes, while a
    steady re-poll of the same file stays idempotent.
    """
    digest = hashlib.sha256(raw).hexdigest()
    try:
        st = path.stat()
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
        size = st.st_size
    except OSError:
        mtime_ns = "unknown"
        size = len(raw)
    return f"sha256:{digest};mtime_ns:{mtime_ns};size:{size}"


@dataclass(frozen=True)
class TerminalSignalObservation:
    """Side-effect-free result of reading one fenced terminal-signal artifact."""

    status: str
    payload: dict | None = None
    path: str | None = None
    reason: str | None = None


def observe_terminal_signal(node, binding, *, runtime_root=None) -> TerminalSignalObservation:
    """Read and fence the terminal signal without journaling or quarantining.

    Hook subprocesses use this seam so they can classify turn-end state without ever becoming
    ledger writers. The daemon-facing :func:`read_terminal_signal` wraps this observation and
    retains the existing malformed-artifact containment behavior.
    """
    path = _signal_path(node, runtime_root=runtime_root)
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except FileNotFoundError:
        return TerminalSignalObservation(status="absent", path=str(path))
    except (UnicodeDecodeError, OSError) as exc:
        return TerminalSignalObservation(
            status="malformed",
            path=str(path),
            reason=f"unreadable artifact: {type(exc).__name__}: {exc}",
        )
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return TerminalSignalObservation(
            status="malformed",
            path=str(path),
            reason=f"invalid JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return TerminalSignalObservation(
            status="malformed",
            path=str(path),
            reason=f"JSON payload is {type(payload).__name__}, not an object",
        )
    if payload.get("owner_token") != binding.get("owner_token"):
        return TerminalSignalObservation(status="stale", path=str(path))
    payload["_signal_artifact_seen_at"] = _signal_artifact_identity(path, raw)
    return TerminalSignalObservation(status="valid", payload=payload, path=str(path))


def _quarantine_invalid_signal(path, node, reason: str) -> None:
    """Contain a MALFORMED agent-written .signal artifact (RR-2): journal + quarantine, never crash.

    The artifact is untrusted agent input (the agent writes it inside its own jail-writable node
    dir, F19; the tmp+rename instruction is advisory). A torn/invalid file used to either crash
    the daemon process (detector.liveness path — a deterministic relaunch crash-loop, the poison
    file surviving every relaunch) or be swallowed silently per tick (the watchdog path). The
    WATCHDOG §7 torn-artifact rule is "rejected, not adopted" — so the caller treats it as
    no-actionable-signal — but the rejection is made VISIBLE:

      * ONE best-effort ``signal_artifact_invalid`` run-ledger row (via the SWL-01 locked
        ``executor.journal``) names the node + the parse fault, and
      * the artifact is renamed to ``<name>.invalid`` (best-effort) so the agent/operator can
        inspect it and the next tick does not re-trip — the rename IS the edge-trigger.

    Distinct from MissingTranscriptPath (a daemon-side spawn<->detector CONTRACT violation, which
    stays deliberately fail-loud): agent-supplied bytes must degrade to a journaled rejection.
    """
    try:
        from . import executor as _executor_mod  # local import: keep this module import-light

        _executor_mod.journal(
            _node_address(node),
            event="signal_artifact_invalid",
            binding_delta={"artifact": str(path), "error": reason},
            summary=(
                f"malformed terminal-signal artifact for {_node_address(node)}: {reason} — "
                "rejected (not adopted, WATCHDOG §7); quarantined to *.invalid"
            ),
        )
    except Exception:  # noqa: BLE001 — the journal is best-effort; rejection must never crash
        pass
    try:
        path.rename(path.with_name(path.name + ".invalid"))
    except OSError:
        pass  # quarantine is best-effort; a re-trip next tick re-journals (still contained)


def read_terminal_signal(node, binding) -> dict | None:
    """Read the FENCED terminal-signal artifact for a node (§2.8 / §4.1).

    Returns {signal, ts, owner_token, evidence, _signal_artifact_seen_at} IFF the on-disk
    .signal.json exists AND its owner_token EQUALS the live binding's owner_token (current epoch).
    Otherwise None:

      * absent .signal.json (or absent node dir)            -> None
      * a STALE owner_token (a prior incarnation's leftover) -> None  (THE LOAD-BEARING FENCE:
        a dead incarnation's leftover signal must NEVER collapse a re-spawned node at the same
        address — the watchdog journals stale_return_ignored and falls through to liveness).
      * MALFORMED bytes (invalid JSON / non-UTF-8 / a JSON non-dict — RR-2) -> None, contained
        fail-loud: the fault is journaled (``signal_artifact_invalid``) and the artifact is
        quarantined to ``*.invalid`` — agent-written input must NEVER crash the watchdog tick
        or the detector/reconcile path (WATCHDOG §7: a torn artifact is rejected, not adopted).

    The reader does NOT decide collapse-vs-noop and does NOT filter by signal kind — an ESCALATED
    signal with a live token is returned unchanged (the verdict needs it as the waiting-reason;
    the watchdog routes ESCALATED to NOOP, DONE/FAILED to collapse).
    """
    observation = observe_terminal_signal(node, binding)
    if observation.status == "malformed":
        _quarantine_invalid_signal(
            _signal_path(node),
            node,
            observation.reason or "malformed terminal-signal artifact",
        )
        return None
    if observation.status != "valid":
        return None
    return observation.payload
