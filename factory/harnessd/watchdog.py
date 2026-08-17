"""watchdog — the liveness lifecycle (Increment 11, cluster ②).

The §2.9 watchdog: terminal-signal-FIRST collapse, the leaf idle->prod->FAILED
ladder (the two-counter discipline), the ③-wake trigger, and the coordinator-death
probe. It is NOT a writer — every binding mutation funnels through the REAL
single-writer executor (a COLLAPSE routes through ``chokepoint.collapse``; a
watchdog-imposed FAILED routes through ``executor.transition``). The detector's
liveness verdict is the one injected seam (the within-W timing was validated for
real in Inc 6 + the Inc 9 tmux contract — see the module docstring fork note).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.9 (the FROZEN watchdog.py interface — transcribed into
    the signatures below) + the Increment-11 Done-test (L779-788) + §4.1 battery
    (L564-587).
  - design/WATCHDOG.md §4.1-§4.4 (the sign-off-or-fail path: the JOURNAL the check
    reads / the 3-step sequence / the prod gate (prompt-string match) / verify-new-
    turn / FAILED-via-executor + actor='harnessd' + reason watchdog_nonresponse),
    §3.5 (the two-counter discipline + stale-grace), §5.1/§5.5 (the coordinator
    process-death probe: dead-pid + live-children = recoverable orphan -> ESCALATE).
  - design/DAEMON.md §3.6 (the TERMINAL_VOCAB mapping) — states.TERMINAL_VOCAB.

REUSE (BIAS TO REAL, Lesson 7): the executor + on-disk ledger are REAL; the
.signal.json / .inbox.jsonl are REAL files read by the REAL
``detector_signals.read_terminal_signal`` / the REAL inbox tail; a COLLAPSE/FAILED
routes through the REAL executor (asserted via the REAL ledger). The ONLY injected
mock is the detector liveness verdict.

---------------------------------------------------------------------------------
RESOLVED DETAILS (unspecified by §2.9 / the frozen tests; decided spec-faithfully,
surfaced to the orchestrator — also recorded as forks in the build report):

  * WatchdogAction SHAPE (FORK-WDACTION) — §2.9 names ``WatchdogAction`` as a TAGGED
    action type but fixes no concrete shape (IMPLEMENTATION-PLAN L79 lists it among
    "result types … refinement"). v1 ships it as a frozen dataclass with a ``kind``
    tag (one of COLLAPSE / NOOP / PROD / FAILED / ESCALATE / WAKE / WAIT) plus the
    parent-directed ``target`` (the parent address an ESCALATE/FAILED is routed to)
    and a free-form ``detail`` dict. The frozen test reads the tag robustly
    (kind/tag/action/…); ``kind`` is the canonical field. A FAILED leaf's action
    carries BOTH kind='FAILED' AND target=<parent_address> so the parent-directed
    closing action is legible (the test asserts the parent address appears in the
    action repr/dict). FORK: a tagged-enum or a per-action subclass hierarchy would
    also satisfy §2.9; the single dataclass-with-kind reuses ONE result type, the
    precedent the codebase already follows for SpawnResult/ReconcileReport.

  * THE GOLDEN IDLE-PROMPT STRING (FORK-PROMPT) — §2.9 / WATCHDOG §4.3 pin the prod
    gate as "capture-pane shows the idle input prompt (golden string per CC version)"
    but leave the literal string KNOWN-OPEN (it is CC-version-specific and measured at
    commissioning). v1 carries the placeholder golden string ``FORK_PROMPT`` (the CC
    idle-prompt marker) and gates ``prod_precondition`` on a substring match against
    the captured pane. Because the real capture-pane wire is ③'s (not yet wired) and
    the tests drive ``prod_precondition`` through the module seam, the literal is a
    documented placeholder, swapped for the measured CC string at commissioning.

  * HOW LIVENESS IS INJECTED (FORK-LIVENESS-SEAM) — §2.9 has check_leaf read
    ``liveness(node)`` but fixes no injection style. Precedent in this codebase is a
    module-level injectable (``ledger.RUNTIME_ROOT``, ``chokepoint.set_adapter``). v1
    exposes BOTH a module-level ``set_liveness(fn)`` / ``LIVENESS`` seat AND, when no
    override is bound, calls the live ``detector.liveness`` attribute directly (the
    detector module is the production source). check_leaf accepts a node dict (or a
    bare address) and resolves the address before calling the verdict, so the frozen
    test's belt-and-suspenders monkeypatch of ``detector.liveness`` is honored too.

  * WAKE_KEYSTROKE PAYLOAD (FORK-WAKE) — §2.9 pins it as a POINTER ("re-read
    <node>/.inbox.jsonl, resume"), NEVER a fact. The notification names the
    unconsumed-row total and ordered per-sender counts from the same inbox
    snapshot whose covered offset becomes the eventual receipt. The message
    body is NEVER stuffed in. The leaf nonresponse PROD is a separate
    jurisdiction and speaks the turn-end production contract instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import (
    config,
    clock,
    detector,
    detector_signals,
    ledger,
    return_contract,
    review_dispatch,
    runtime_failures,
    turn_state,
)
from .spawn import chokepoint


# ===========================================================================
# WatchdogAction — the §2.9 TAGGED result type (FORK-WDACTION; see module docstring).
# ===========================================================================

@dataclass(frozen=True)
class WatchdogAction:
    """A tagged watchdog action (§2.9). ``kind`` is the canonical tag.

    ``kind``   — one of COLLAPSE / NOOP / PROD / FAILED / ESCALATE / WAKE / WAIT.
    ``node``   — the node_address the action concerns.
    ``target`` — the parent address an ESCALATE/FAILED is routed to (None otherwise);
                 makes a FAILED/ESCALATE closing action legibly parent-directed.
    ``detail`` — a free-form dict carrying the action's evidence (terminal_signal,
                 liveness state, reason, …) for the journal / cluster-② handler.
    """

    kind: str
    node: Optional[str] = None
    target: Optional[str] = None
    detail: dict = field(default_factory=dict)


# Canonical tags (one place, so a typo at a construction site fails loud on the enum-ish set).
COLLAPSE = "COLLAPSE"
NOOP = "NOOP"
PROD = "PROD"
FAILED = "FAILED"
ESCALATE = "ESCALATE"
WAKE = "WAKE"
WAIT = "WAIT"
BLOCKED_INPUT_SILENCE_SECONDS = 300


# ===========================================================================
# The liveness injection seam (FORK-LIVENESS-SEAM). A module-level override that,
# when unset, falls through to the live detector.liveness attribute.
# ===========================================================================

LIVENESS: Optional[Callable[[str], "detector.Liveness"]] = None


def set_liveness(fn: Optional[Callable[[str], "detector.Liveness"]]) -> None:
    """Inject the liveness verdict function (module-level seam; precedent: set_adapter)."""
    global LIVENESS
    LIVENESS = fn


def _liveness(node_address: str):
    """Resolve the liveness verdict for ``node_address`` through the seam, else detector.liveness."""
    if LIVENESS is not None:
        return LIVENESS(node_address)
    # No override: call the LIVE detector.liveness attribute (so a test that monkeypatches
    # detector.liveness is honored without this module holding a stale local copy).
    return detector.liveness(node_address)


# ===========================================================================
# The golden idle-prompt gate string (FORK-PROMPT) — MEASURED on the pinned CC v2.1.152
# (commissioning probe 2026-06-10; captured fixture tests/fixtures/cc-2.1.152-idle-pane.txt).
# The idle pane renders an input line beginning '❯' with a '? for shortcuts' status line
# below. Pinned per CC version (WATCHDOG §4.3 / §8): a CC bump re-measures this string.
# ===========================================================================

FORK_PROMPT: str = "❯"  # the CC v2.1.152 idle-input-prompt marker (measured, fixture-pinned)

# E4 — the PER-RUNTIME prompt-marker set: the Codex 0.128.0 TUI renders its idle composer
# with '›' (probed live 2026-06-11), CC with '❯'. The gates accept EITHER marker (any-match):
# a runtime-keyed lookup would need the binding threaded into every gate call site; the
# any-match set is deterministic, and the dialog/working refusals above it still close the
# gate on every unsafe pane state. A new runtime adds its measured marker here.
PROMPT_MARKERS: tuple = ("❯", "›")

# CC renders the '❯' input box even WHILE GENERATING (steering is allowed mid-run), so the
# prompt char alone cannot distinguish idle from busy. The working marker below is the busy
# signal CC shows during generation/tool-calls — its presence CLOSES the gate (§4.3
# Precondition 1: a send-keys nudge mid-turn corrupts the input line). Mirrors the proven
# interactive_eval._WORKING_MARKERS member.
_WORKING_MARKER: str = "esc to interrupt"

# CC's blocking DIALOGS (trust prompt, tool-approval prompt, bypass warning, selection menus)
# ALSO render '❯' as the selection cursor (probed live: the trust dialog shows '❯ 1. Yes, I
# trust this folder' / 'Enter to confirm · Esc to cancel'; the tool-approval dialog shows
# '❯ 1. Yes' / 'Esc to cancel · Tab to amend'). A nudge typed into a dialog would press Enter
# ON the selection — confirming whatever is highlighted. Deterministic trust + the jail's
# skip-permissions make dialogs structurally absent in production, but the gate refuses them
# anyway (belt-and-braces; both dialogs fixture-pinned).
_DIALOG_MARKERS: tuple[str, ...] = ("Enter to confirm", "Esc to cancel")


# ===========================================================================
# Small helpers — address extraction; the W(state) suspicion window; the
# capture-pane read (③'s wire, stubbed behind a module seam here).
# ===========================================================================

def _node_address(node_or_address) -> str:
    """The stable address from a node dict or a bare address string."""
    if isinstance(node_or_address, str):
        return node_or_address
    return node_or_address["node_address"]


def _w_window(binding: dict) -> int:
    """The W(state) suspicion window (seconds) for this node — keyed by suspicion_window_key (§3.3).

    Mirrors detector._w_window: read the dedicated ``suspicion_window_key`` the spawn step sets,
    floor to the tightest ``working`` window (earliest suspicion — safe) when absent/unknown.
    """
    key = binding.get("suspicion_window_key") or "working"
    return config.SUSPICION_WINDOWS.get(key, config.SUSPICION_WINDOWS["working"])


def _age_beyond_w(last_progress_at: Optional[str], window: int, *, now: Optional[str]) -> bool:
    """True iff last_progress_at is OVERDUE — strictly more than W seconds in the past (§3.3: age > W).

    A None/absent last_progress_at carries no proof of recent progress; treat it as overdue (the
    node has shown no forward progress to read as within-W). Routes through the canonical clock so
    the comparison is offset-invariant.
    """
    if not last_progress_at:
        return True
    return clock.age_seconds(last_progress_at, now=now) > window


def _file_mtime_iso(path: Path) -> Optional[str]:
    try:
        if not path.is_file():
            return None
        timestamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _codex_worker_home(transcript_path: Optional[str]) -> Optional[Path]:
    if not transcript_path:
        return None
    transcript = Path(transcript_path)
    for parent in transcript.parents:
        if parent.name == "sessions":
            return parent.parent
    return None


def _life_evidence_candidates(
    node_address: str,
    node: dict,
    binding: dict,
) -> list[tuple[str, str]]:
    """Collect the existing S5 turn/transcript/runtime-log life artifacts once."""
    candidates: list[tuple[str, str]] = []
    if ledger.RUNTIME_ROOT is not None:
        observation = turn_state.read_current(
            node_address,
            binding,
            runtime_root=ledger.RUNTIME_ROOT,
        )
        if observation.status == "valid":
            updated_at = (observation.payload or {}).get("updated_at")
            if updated_at:
                candidates.append(("turn_state", str(updated_at)))

    transcript_path = node.get("transcript_path") or binding.get("transcript_path")
    transcript_mtime = _file_mtime_iso(Path(transcript_path)) if transcript_path else None
    if transcript_mtime:
        candidates.append(("transcript", transcript_mtime))

    if binding.get("runtime") == "codex" or binding.get("codex_seat_id"):
        worker_home = _codex_worker_home(transcript_path)
        if worker_home is not None:
            for name in ("logs_2.sqlite", "logs_2.sqlite-wal"):
                log_mtime = _file_mtime_iso(worker_home / name)
                if log_mtime:
                    candidates.append(("codex_runtime_log", log_mtime))
    return candidates


def _fresh_life_evidence(
    node_address: str,
    node: dict,
    binding: dict,
    *,
    now: str,
) -> Optional[tuple[str, str]]:
    """Return the freshest existing durable life artifact still inside W."""
    candidates = _life_evidence_candidates(node_address, node, binding)

    fresh: list[tuple[str, str]] = []
    for label, timestamp in candidates:
        try:
            if clock.age_seconds(timestamp, now=now) <= _w_window(binding):
                fresh.append((label, timestamp))
        except (TypeError, ValueError):
            continue
    if not fresh:
        return None
    return max(fresh, key=lambda item: clock.parse_iso(item[1]))


def _latest_life_evidence(
    node_address: str,
    node: dict,
    binding: dict,
) -> Optional[tuple[str, str]]:
    valid: list[tuple[str, str]] = []
    for label, timestamp in _life_evidence_candidates(node_address, node, binding):
        try:
            clock.parse_iso(timestamp)
        except (TypeError, ValueError):
            continue
        valid.append((label, timestamp))
    if not valid:
        return None
    return max(valid, key=lambda item: clock.parse_iso(item[1]))


def latest_life_evidence(
    node_address: str,
    node: dict,
    binding: dict,
) -> Optional[tuple[str, str]]:
    """PUBLIC accessor for the freshest durable life artifact ``(label, timestamp)``, window-free.

    The daemon's run-level quiescence read needs the SAME turn/transcript/runtime-log evidence the
    stall probe fuses — a second implementation would drift from it silently. Unlike
    ``_fresh_life_evidence`` this applies no W filter: the caller compares successive reads rather
    than asking whether one read is recent.
    """
    return _latest_life_evidence(node_address, node, binding)


def _pane_excerpt(pane_text: str) -> str:
    lines = [" ".join(line.split()) for line in str(pane_text or "").splitlines()]
    visible = [line for line in lines if line]
    return " | ".join(visible[-8:])[-800:]


def classify_blocked_on_input(
    turn_payload: dict,
    *,
    latest_life_evidence_at: Optional[str],
    pane_alive: bool,
    pane_pid: Optional[int],
    descendant_cpu: Optional[float],
    pane_text: str,
    prompt_signature: Optional[str],
    now: str,
    silence_window_seconds: int = BLOCKED_INPUT_SILENCE_SECONDS,
) -> dict:
    """Pure three-fact + positive-prompt classifier; only one result permits Escape."""
    state = str((turn_payload or {}).get("state") or "")
    in_flight = list((turn_payload or {}).get("in_flight_tools") or [])
    if state not in {"tool_in_flight", "waiting_on_human"} or not in_flight:
        return {"classification": "healthy", "reason": "no_in_flight_tool"}
    if not latest_life_evidence_at:
        return {"classification": "unknown", "reason": "life_evidence_timestamp_unknown"}
    try:
        silent_seconds = float(clock.age_seconds(latest_life_evidence_at, now=now))
    except (TypeError, ValueError):
        return {"classification": "unknown", "reason": "life_evidence_timestamp_unknown"}
    if silent_seconds < float(silence_window_seconds):
        return {
            "classification": "healthy",
            "reason": "life_evidence_within_window",
            "silent_seconds": max(0.0, silent_seconds),
        }
    if not pane_alive or pane_pid is None:
        return {"classification": "unknown", "reason": "pane_not_live"}
    if descendant_cpu is None:
        return {"classification": "unknown", "reason": "process_probe_unknown"}
    if float(descendant_cpu) > 0.0:
        return {
            "classification": "healthy",
            "reason": "active_descendant_process",
            "descendant_cpu": float(descendant_cpu),
        }
    classification = (
        "blocked_on_input"
        if prompt_signature
        else "silent_in_flight_unconfirmed"
    )
    return {
        "classification": classification,
        "reason": (
            "silent_zero_cpu_prompt"
            if prompt_signature
            else "prompt_signature_absent"
        ),
        "detected_at": str(now),
        "last_life_evidence_at": str(latest_life_evidence_at),
        "silent_seconds": max(0.0, silent_seconds),
        "pane_pid": int(pane_pid),
        "descendant_cpu": float(descendant_cpu),
        "prompt_signature": prompt_signature,
        "pane_excerpt": _pane_excerpt(pane_text),
    }


def probe_blocked_on_input(node: dict, binding: dict, *, now: str) -> dict:
    """Read the existing S5 artifacts and the runtime-owned prompt vocabulary."""
    node_address = _node_address(node)
    if ledger.RUNTIME_ROOT is None:
        return {"classification": "unknown", "reason": "runtime_root_unbound"}
    observation = turn_state.read_current(
        node_address,
        binding,
        runtime_root=ledger.RUNTIME_ROOT,
    )
    if observation.status != "valid":
        return {
            "classification": "unknown",
            "reason": f"turn_state_{observation.status}",
        }
    payload = observation.payload or {}
    evidence = _latest_life_evidence(node_address, node, binding)
    latest_at = evidence[1] if evidence else None
    # Most seats are either outside a tool call or still producing ordinary life evidence. Let the
    # pure classifier reject those cheap cases before paying for a process-table snapshot and pane
    # capture on every seat in every daemon poll. Supplying a deliberately absent pane here reaches
    # ``pane_not_live`` only when the in-flight + silence prerequisites genuinely require the two
    # remaining reads.
    prerequisite = classify_blocked_on_input(
        payload,
        latest_life_evidence_at=latest_at,
        pane_alive=False,
        pane_pid=None,
        descendant_cpu=None,
        pane_text="",
        prompt_signature=None,
        now=now,
    )
    if prerequisite.get("reason") != "pane_not_live":
        return prerequisite
    try:
        alive, pane_pid = detector_signals.pane_alive(node)
    except Exception:  # noqa: BLE001 — UNKNOWN never authorizes a key
        alive, pane_pid = False, None
    descendant_cpu = (
        detector_signals.pane_pid_cpu(node, pane_pid)
        if alive and pane_pid is not None
        else None
    )
    pane_text = _capture_pane(node) if alive else ""
    adapter = chokepoint.ADAPTER or chokepoint.ADAPTER_REGISTRY.get(binding.get("runtime"))
    matcher = getattr(adapter, "interactive_prompt_signature", None)
    signature = matcher(pane_text) if callable(matcher) else None
    return classify_blocked_on_input(
        payload,
        latest_life_evidence_at=latest_at,
        pane_alive=alive,
        pane_pid=pane_pid,
        descendant_cpu=descendant_cpu,
        pane_text=pane_text,
        prompt_signature=signature,
        now=now,
    )


def _capture_pane(node) -> str:
    """Capture the node's pane text (the prod-gate evidence) — the REAL ③ wire.

    Reads the live pane buffer via ``tmux.capture_pane(node["tmux_target"])`` — the canonical
    ``<session>:<window>.<pane>`` triple the F18 fix records on the binding. This stays the ONE
    module-level seam the tests monkeypatch (same name, same ``node -> str`` shape as the v1
    stub), so the existing prod-gate tests drive it unchanged.

    CONSERVATIVE on every failure mode: a node with no tmux_target, a gone pane, or a capture
    error reads as an EMPTY pane -> the gate stays CLOSED (never prod un-gated). The local
    import keeps the module import-light (the daemon binds the tmux socket seam at boot).
    """
    target = node.get("tmux_target") if isinstance(node, dict) else None
    if not target:
        return ""
    try:
        from .spawn import tmux as _tmux
        return _tmux.capture_pane(target) or ""
    except Exception:  # noqa: BLE001 — an unreadable pane is gate-closed evidence, not a crash
        return ""


# ===========================================================================
# STEP A + STEP B — check_leaf (the §2.9 leaf path).
# ===========================================================================

def _reset_consumed_terminal(node_address: str, binding: dict) -> None:
    """Reset the address death streak only after a fenced signal was accepted."""
    chokepoint.reset_actor_death_streak(
        node_address,
        expected_owner_token=binding.get("owner_token"),
    )


def check_terminal_signal(node, binding, *, allow_collapse: bool = True) -> Optional[WatchdogAction]:
    """STEP A, shared by leaves AND coordinators (the 2026-06-11 live-run wedge): the fenced
    terminal-signal-first check. The §5.4 leaf/coordinator split exempts coordinators from the
    idle LADDER only — truth-recording (a fenced DONE/FAILED/ESCALATED sign-off) applies to EVERY
    node; before this extraction a coordinator's DONE was never read and the upward path froze
    with the whole subtree green.

    ``allow_collapse=False`` is the coordinator-with-live-descendants mode: a live-child coordinator
    must not collapse on DONE/FAILED, but ESCALATED is not a collapse and still has to journal so a
    later parent question wakes upward. Returns the enacted action (COLLAPSE, or a NOOP carrying
    collapse_failed / escalate_journal_failed / escalated_holds_slot), or None when there is no
    actionable signal (absent / stale-fenced / unrecognized kind / ESCALATED-and-answered) — the
    caller falls through to its own liveness policy (leaf: the idle ladder; coordinator: the death
    probe). The CALLER owns the live-descendant gate (never collapse over live children —
    agent-lifecycle's bottom-up shutdown)."""
    node_address = _node_address(node)
    sig = detector_signals.read_terminal_signal(node, binding)
    if sig is None:
        return None
    signal = sig.get("signal")
    if signal != "ESCALATED" and not allow_collapse:
        return None
    if signal == "ESCALATED":
        # ESCALATED holds its slot — NEVER collapses (§3.6 asymmetric). But the slot-hold is
        # JOURNALED (SML-02): chokepoint.escalate stamps terminal_signal=ESCALATED + appends the
        # signal_ESCALATED running->running row through the single-writer executor, exactly once
        # PER ARTIFACT (SM-4/LR-25: idempotency keys on the harness-observed artifact identity,
        # never the agent-authored ts — a post-answer SECOND question re-journals; a re-poll of
        # the same artifact is a no-op).
        # ROUTE THE RESULT: a fenced/CAS-aborted journal write must NOT read as a clean
        # slot-hold — the next tick retries against the still-present .signal artifact
        # (mirrors the collapse_failed routing below).
        result = chokepoint.escalate(
            node_address, expected_owner_token=binding.get("owner_token"),
            signal_artifact_seen_at=sig.get("_signal_artifact_seen_at"),
        )
        if result is not None and getattr(result, "ok", True) is False:
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": "escalate_journal_failed", "terminal_signal": signal,
                        "errors": list(getattr(result, "errors", []) or [])},
            )
        if result is not None or not detector_signals.escalation_answered(
            binding, sig.get("_signal_artifact_seen_at")
        ):
            _reset_consumed_terminal(node_address, binding)
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": "escalated_holds_slot", "terminal_signal": signal,
                        "journaled": result is not None},
            )
        # SM-4: the round-trip for THIS artifact is ANSWERED (answered_at is fresher than the
        # stamp and this exact artifact has already been journaled) — the slot-hold no longer shields the node. Return
        # None so the caller falls through (leaf: STEP 0/STEP B revives the ladder post-answer,
        # WATCHDOG §3.5); a NEW question (a fresh artifact) re-journals above and re-arms the hold.
        return None
    if signal == "DONE":
        # E2 — THE RETURN-CONTRACT WALKER (enforcement spine): a DONE sign-off whose return
        # artifacts fail the deterministic floor (report.md present; given requirement IDs cited;
        # trace stanzas parse) is REFUSED — the collapse does not run, ONE edge-triggered typed-
        # defect row + ONE inbox defect line land (the ③-wake nudges the agent to fix + re-signal).
        # FAILED/ESCALATED are exempt (never trap an agent in its own refusal loop). This makes the
        # role docs' "the hook rejects it — you cannot report complete" true at runtime (LR-14).
        verdict = return_contract.check_done_contract(node_address, binding)
        if not verdict.ok:
            return_contract.journal_defects_once(
                node_address,
                binding,
                sig.get("_signal_artifact_seen_at"),
                verdict.defects,
                agent_signal_ts=sig.get("ts"),
            )
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": "return_contract_failed", "terminal_signal": signal,
                        "defects": list(verdict.defects)},
            )
        if binding.get("gate_for"):
            gate_verdict = _gate_review_verdict(sig)
            report_verdict = return_contract.gate_report_verdict(node_address, binding)
            if gate_verdict in {"ACCEPT", "BOUNCE", "ESCALATE"} and report_verdict and gate_verdict != report_verdict:
                defect = (
                    f"GATE-VERDICT-MISMATCH: terminal signal verdict {gate_verdict} "
                    f"does not match authoritative gate artifact verdict {report_verdict} "
                    f"for candidate {binding.get('gate_for')}"
                )
                return_contract.journal_defects_once(
                    node_address,
                    binding,
                    sig.get("_signal_artifact_seen_at"),
                    [defect],
                    agent_signal_ts=sig.get("ts"),
                )
                return WatchdogAction(
                    kind=NOOP, node=node_address,
                    detail={"reason": "gate_verdict_mismatch", "terminal_signal": signal,
                            "candidate": binding.get("gate_for"), "verdict": gate_verdict,
                            "report_verdict": report_verdict},
                )
            if gate_verdict in {"ACCEPT", "BOUNCE", "ESCALATE"}:
                defect = _gate_candidate_identity_defect(binding, sig)
                if defect:
                    return_contract.journal_defects_once(
                        node_address,
                        binding,
                        sig.get("_signal_artifact_seen_at"),
                        [defect],
                        agent_signal_ts=sig.get("ts"),
                    )
                    return WatchdogAction(
                        kind=NOOP, node=node_address,
                        detail={"reason": "gate_candidate_identity_mismatch",
                                "terminal_signal": signal,
                                "candidate": binding.get("gate_for"), "verdict": gate_verdict,
                                "defects": [defect]},
                    )
                manifest_defects = review_dispatch.candidate_artifact_manifest_defects_for_review(binding)
                if manifest_defects:
                    return_contract.journal_defects_once(
                        node_address,
                        binding,
                        sig.get("_signal_artifact_seen_at"),
                        manifest_defects,
                        agent_signal_ts=sig.get("ts"),
                    )
                    chokepoint.fail_gate_candidate_artifact_drift(
                        node_address,
                        defects=manifest_defects,
                    )
                    return WatchdogAction(
                        kind=NOOP, node=node_address,
                        detail={"reason": "candidate_artifact_drift",
                                "terminal_signal": signal,
                                "candidate": binding.get("gate_for"), "verdict": gate_verdict,
                                "defects": list(manifest_defects)},
                    )
            prior_defects = return_contract.refusal_for_signal_artifact(
                node_address, sig.get("_signal_artifact_seen_at")
            )
            if prior_defects is not None:
                return WatchdogAction(
                    kind=NOOP, node=node_address,
                    detail={"reason": "return_contract_failed", "terminal_signal": signal,
                            "defects": list(prior_defects), "signal_artifact_previously_refused": True},
                )
            if gate_verdict == "ACCEPT":
                result = chokepoint.pass_gate(
                    node_address,
                    expected_owner_token=binding.get("owner_token"),
                    signal_artifact_seen_at=sig.get("_signal_artifact_seen_at"),
                    verdict_notes=_gate_review_notes(sig),
                )
                if result is not None and getattr(result, "ok", True) is False:
                    return WatchdogAction(
                        kind=NOOP, node=node_address,
                        detail={"reason": "gate_pass_failed", "terminal_signal": signal,
                                "errors": list(getattr(result, "errors", []) or [])},
                    )
                if result is None:
                    _reset_consumed_terminal(node_address, binding)
                    return WatchdogAction(
                        kind=NOOP, node=node_address,
                        detail={"reason": "gate_pass_already_committed", "terminal_signal": signal},
                    )
                _reset_consumed_terminal(node_address, binding)
                return WatchdogAction(
                    kind=COLLAPSE, node=node_address,
                    detail={"reason": "gate_passed", "terminal_signal": signal,
                            "candidate": binding.get("gate_for"), "verdict": gate_verdict},
                )
            if gate_verdict == "BOUNCE":
                result = chokepoint.bounce_gate(
                    node_address,
                    expected_owner_token=binding.get("owner_token"),
                    signal_artifact_seen_at=sig.get("_signal_artifact_seen_at"),
                    verdict_notes=_gate_review_notes(sig),
                )
                if result is not None and getattr(result, "ok", True) is False:
                    return WatchdogAction(
                        kind=NOOP, node=node_address,
                        detail={"reason": "gate_bounce_failed", "terminal_signal": signal,
                                "errors": list(getattr(result, "errors", []) or [])},
                    )
                if result is None:
                    live_candidate = ledger.read_binding(binding.get("gate_for"))
                    if live_candidate and live_candidate.get("gate_state") == "gate_escalated":
                        reason = "gate_escalation_already_committed"
                    else:
                        reason = "gate_bounce_already_committed"
                else:
                    result_binding = getattr(result, "binding", None) or {}
                    reason = (
                        "gate_escalated"
                        if result_binding.get("gate_state") == "gate_escalated"
                        else "gate_bounced"
                    )
                _reset_consumed_terminal(node_address, binding)
                return WatchdogAction(
                    kind=NOOP, node=node_address,
                    detail={"reason": reason, "terminal_signal": signal,
                            "candidate": binding.get("gate_for"), "verdict": gate_verdict},
                )
            if gate_verdict == "ESCALATE":
                result = chokepoint.escalate_gate(
                    node_address,
                    expected_owner_token=binding.get("owner_token"),
                    signal_artifact_seen_at=sig.get("_signal_artifact_seen_at"),
                    verdict_notes=_gate_review_notes(sig),
                )
                if result is not None and getattr(result, "ok", True) is False:
                    return WatchdogAction(
                        kind=NOOP, node=node_address,
                        detail={"reason": "gate_escalation_failed", "terminal_signal": signal,
                                "errors": list(getattr(result, "errors", []) or [])},
                    )
                _reset_consumed_terminal(node_address, binding)
                return WatchdogAction(
                    kind=NOOP, node=node_address,
                    detail={"reason": "gate_escalated" if result is not None else "gate_escalation_already_committed",
                            "terminal_signal": signal,
                            "candidate": binding.get("gate_for"), "verdict": gate_verdict},
                )
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": "gate_verdict_missing", "terminal_signal": signal,
                        "candidate": binding.get("gate_for"), "verdict": gate_verdict},
            )
        prior_defects = return_contract.refusal_for_signal_artifact(
            node_address, sig.get("_signal_artifact_seen_at")
        )
        if prior_defects is not None:
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": "return_contract_failed", "terminal_signal": signal,
                        "defects": list(prior_defects), "signal_artifact_previously_refused": True},
            )
        if binding.get("gate_required"):
            result = chokepoint.submit_gate_candidate(
                node_address,
                expected_owner_token=binding.get("owner_token"),
                signal_artifact_seen_at=sig.get("_signal_artifact_seen_at"),
            )
            if result is not None and getattr(result, "ok", True) is False:
                return WatchdogAction(
                    kind=NOOP, node=node_address,
                    detail={"reason": "gate_candidate_failed", "terminal_signal": signal,
                            "errors": list(getattr(result, "errors", []) or [])},
                )
            result_binding = getattr(result, "binding", None) if result is not None else None
            live_binding = result_binding or ledger.read_binding(node_address) or {}
            if live_binding.get("gate_state") == "gate_failed":
                reason = "gate_failed" if result is not None else "gate_failed_already_committed"
            elif live_binding.get("gate_state") == "gate_escalated":
                reason = "gate_escalation_already_committed"
            else:
                reason = "gate_candidate_submitted" if result is not None else "gate_candidate_already_submitted"
            review_address = (result_binding or {}).get("gate_review_address") or binding.get(
                "gate_review_address"
            )
            _reset_consumed_terminal(node_address, binding)
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": reason, "terminal_signal": signal,
                        "review": review_address,
                        "failure_class": live_binding.get("gate_failure_class")},
            )
    if signal in ("DONE", "FAILED"):
        # Route the terminal collapse through the REAL chokepoint/executor (running -> done/failed).
        # ROUTE THE RESULT (review watchdog-2): a FAILED terminal transition (a CAS miss / fencing
        # rejection) must NOT be reported as a clean COLLAPSE — that would tell the daemon the node
        # is gone when it is not. On a failed collapse we return a NOOP (collapse_failed) so the next
        # tick retries against the still-present .signal.json; only a SUCCESSFUL collapse is a COLLAPSE.
        result = chokepoint.collapse(
            node_address,
            signal,
            expected_owner_token=binding.get("owner_token"),
        )
        if result is not None and getattr(result, "ok", True) is False:
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": "collapse_failed", "terminal_signal": signal,
                        "errors": list(getattr(result, "errors", []) or [])},
            )
        _reset_consumed_terminal(node_address, binding)
        return WatchdogAction(
            kind=COLLAPSE, node=node_address,
            detail={"terminal_signal": signal, "evidence": sig.get("evidence")},
        )
    # An unrecognized fenced signal kind is not actionable here -> the caller falls through.
    return None


def _gate_review_notes(sig) -> str:
    evidence = sig.get("evidence") if isinstance(sig, dict) else {}
    if not isinstance(evidence, dict):
        return ""
    return str(evidence.get("notes") or "")


def _gate_review_verdict(sig) -> Optional[str]:
    evidence = sig.get("evidence") if isinstance(sig, dict) else {}
    if not isinstance(evidence, dict):
        return None
    explicit = str(evidence.get("verdict") or "").strip().upper()
    if explicit in {"ACCEPT", "BOUNCE", "ESCALATE"}:
        return explicit
    notes = str(evidence.get("notes") or "").upper()
    marker = "VERDICT:"
    if marker not in notes:
        return None
    verdict = notes.split(marker, 1)[1].strip()
    if verdict.startswith("ACCEPT"):
        return "ACCEPT"
    if verdict.startswith("BOUNCE"):
        return "BOUNCE"
    if verdict.startswith("ESCALATE"):
        return "ESCALATE"
    return None


def _gate_candidate_identity_defect(review_binding: dict, sig: dict) -> Optional[str]:
    """Return a defect when a review verdict is not pinned to the current candidate."""
    producer_address = (review_binding or {}).get("gate_for")
    if not producer_address:
        return None
    producer = ledger.read_binding(producer_address) or {}
    expected_gate_id = str(producer.get("gate_id") or "").strip()
    expected_packet = str(producer.get("gate_review_packet") or "").strip()
    expected_signal = str(producer.get("gate_candidate_signal_artifact_seen_at") or "").strip()
    if not (expected_gate_id or expected_packet or expected_signal):
        return None

    evidence = sig.get("evidence") if isinstance(sig, dict) else {}
    if not isinstance(evidence, dict):
        evidence = {}
    actual_gate_id = str(
        evidence.get("gate_id")
        or evidence.get("review_gate_id")
        or evidence.get("gate")
        or ""
    ).strip()
    actual_packet = str(
        evidence.get("review_packet")
        or evidence.get("gate_review_packet")
        or ""
    ).strip()
    actual_signal = str(
        evidence.get("candidate_signal_artifact_seen_at")
        or evidence.get("candidate_signal_artifact_identity")
        or evidence.get("gate_candidate_signal_artifact_seen_at")
        or ""
    ).strip()

    if (
        (expected_gate_id and actual_gate_id == expected_gate_id)
        or (expected_packet and actual_packet == expected_packet)
        or (expected_signal and actual_signal == expected_signal)
    ):
        return None
    expected = expected_gate_id or expected_packet or expected_signal
    if not (actual_gate_id or actual_packet or actual_signal):
        return (
            f"MISSING-GATE-CANDIDATE-IDENTITY: review seat {review_binding.get('node_address')} "
            f"signed DONE for {producer_address} without naming the current gate packet ({expected})"
        )
    return (
        f"GATE-CANDIDATE-IDENTITY-MISMATCH: review seat {review_binding.get('node_address')} "
        f"signed for gate_id={actual_gate_id or '-'} review_packet={actual_packet or '-'} "
        f"candidate_signal={actual_signal or '-'}, but current candidate {producer_address} "
        f"is gate_id={expected_gate_id or '-'} review_packet={expected_packet or '-'} "
        f"candidate_signal={expected_signal or '-'}"
    )


def check_leaf(node, binding, *, now) -> WatchdogAction:
    """The leaf liveness check (§2.9): terminal-signal FIRST, then the idle->prod->FAILED ladder.

    STEP A (TERMINAL-SIGNAL FIRST — the producer for INCLUDE-item #3):
        sig = detector_signals.read_terminal_signal(node, binding)
          * sig present & FENCED (owner_token matches):
              - DONE / FAILED  -> COLLAPSE (route to chokepoint.collapse through the REAL executor);
              - ESCALATED      -> journal signal_ESCALATED + stamp terminal_signal (exactly once,
                via chokepoint.escalate through the REAL executor), then NOOP — ESCALATED HOLDS ITS
                SLOT, never collapses (§2.3/§3.6 asymmetric). A failed journal write is routed
                (reason=escalate_journal_failed) and retried next tick.
          * sig present but STALE owner_token -> read_terminal_signal returns None (the fence): we
            fall through to STEP B (the live binding is UNCHANGED; the dead incarnation's leftover
            signal NEVER collapses the re-spawned node). The executor journals stale_return_ignored
            if a stale return is later presented; here the reader simply yields None.

    STEP 0 (WATCHDOG §3.4 — between STEP A and STEP B, the ratified placement): a PAUSED subtree
        (this node OR any ancestor carries paused_at) gets NO recovery action — no suspicion, no
        stale-counter advance, no prod, no watchdog-imposed FAILED -> NOOP (paused_subtree). The
        agent's own fenced terminal sign-off (STEP A, above) is still honored while paused:
        truth-recording is not a recovery action.

    STEP B (no actionable signal): read the liveness verdict.
        * idle + age>W + within grace -> PROD (gated by prod_precondition);
        * idle + age>W + AT/over grace -> FAILED (the ladder exhausted);
        * anything else (working/waiting/dead/within-W) -> NOOP.

    CLOSING ACTION on FAILED (v1 floor, INCLUDE-item #5): mark running->failed via the REAL
    executor (event='watchdog_nonresponse', reason carried so the row is distinguishable from an
    agent-self-emitted FAILED; actor='harnessd' is the executor's single-writer stamp) AND ESCALATE
    TO THE PARENT (the returned action carries kind='FAILED' + target=parent_address). v1 does NOT
    auto-respawn from harnessd (the lease-recovery state machine + auto_resume_command are DEFERRED;
    the auto_resume_command field is left UNREAD on the leaf leg).
    """
    node_address = _node_address(node)

    # ----- STEP A: terminal-signal FIRST (fenced reader; a stale token yields None). Extracted to
    # check_terminal_signal so COORDINATORS share it (the 2026-06-11 live-run wedge: the §5.4 split
    # exempts coordinators from the LADDER only — truth-recording applies to EVERY node). -----
    signal_action = check_terminal_signal(node, binding)
    if signal_action is not None:
        return signal_action
    # ----- STEP 0 (WATCHDOG §3.4): a PAUSED subtree (this node OR any ancestor) gets NO recovery
    # actions — no suspicion, no stale-counter advance, no prod, no watchdog-imposed FAILED.
    # Placed AFTER STEP A (the ratified §3.4 placement): the agent's own fenced terminal sign-off
    # is truth-recording, not a recovery action, and is still honored while paused. Reuses the
    # chokepoint's ONE node-or-ancestor predicate so the two read-points cannot drift. -----
    if chokepoint.subtree_paused(node_address):
        return WatchdogAction(
            kind=NOOP, node=node_address,
            detail={"reason": "paused_subtree"},
        )

    # ----- STEP B: the idle -> prod -> FAILED ladder (no actionable terminal signal) -----
    verdict = _liveness(node_address)
    state = getattr(verdict, "state", None)
    last_progress_at = getattr(verdict, "last_progress_at", None)

    # Only an IDLE verdict is actionable (working/waiting/dead are NOT prodded/failed here):
    #   - working / waiting -> the node is fine / holding its slot -> NOOP;
    #   - dead              -> the process is gone; the leaf-necro is ①'s mechanical reconcile reap
    #                          (WATCHDOG §4.4) — the watchdog does NOT prod a dead pane -> NOOP here.
    if state != "idle":
        # Reset-on-recovery (the two-counter discipline): a node that recovered to working/waiting
        # must drop its accrued stale_check_count so a later idle spell starts the ladder fresh — a
        # node that blips idle-then-recovers must NOT march toward FAILED on stale prods. The
        # executor.watchdog_checkpoint resets the counter to 0 on a healthy observation (edge-triggered:
        # a no-op when the counter is already 0).
        if state in ("working", "waiting") and (binding.get("stale_check_count") or 0) != 0:
            _checkpoint(node_address, binding, liveness_state=state, last_progress_at=last_progress_at)
        return WatchdogAction(
            kind=NOOP, node=node_address,
            detail={"reason": "not_idle", "liveness_state": state},
        )

    stale_check_count = binding.get("stale_check_count", 0) or 0
    stale_grace_checks = binding.get("stale_grace_checks", 2)
    if stale_grace_checks is None:
        stale_grace_checks = 2
    runtime_failure = (
        detector_signals.runtime_failure_from_transcript(node)
        if stale_check_count >= stale_grace_checks
        else None
    )

    # A warm pane can look idle while a long model turn is still streaming or immediately after a
    # truthful turn-end edge. Reuse the durable artifacts each runtime already writes. Process death
    # has already won in the detector (state == dead never reaches this branch); fresh evidence only
    # recalibrates the idle ladder. A typed runtime failure already found at the exhausted rung wins
    # over the file write that carried that failure; an error record is not proof of healthy work.
    evidence_node = (
        node
        if isinstance(node, dict)
        else {
            "node_address": node_address,
            "transcript_path": binding.get("transcript_path"),
            "tmux_target": binding.get("tmux_target"),
        }
    )
    life_evidence = (
        None
        if runtime_failure is not None
        else _fresh_life_evidence(
            node_address,
            evidence_node,
            binding,
            now=now or clock.now_utc(),
        )
    )
    if life_evidence is not None:
        evidence_label, evidence_at = life_evidence
        _checkpoint(
            node_address,
            binding,
            liveness_state="working",
            last_progress_at=evidence_at,
            last_evidence=evidence_label,
        )
        return WatchdogAction(
            kind=NOOP,
            node=node_address,
            detail={
                "reason": "life_evidence_within_w",
                "liveness_state": "working",
                "last_evidence": evidence_label,
                "last_progress_at": evidence_at,
            },
        )

    # Idle: only ACT once W has elapsed (the false-idle guard is already inside the liveness verdict;
    # this is the watchdog-side age>W gate the §2.9 ladder names explicitly).
    if not _age_beyond_w(last_progress_at, _w_window(binding), now=now):
        return WatchdogAction(
            kind=NOOP, node=node_address,
            detail={"reason": "idle_within_w", "liveness_state": state},
        )

    # The two-counter ladder (§3.5 / §4.3): bounded prods, THEN FAILED at grace.
    if stale_check_count >= stale_grace_checks:
        # The prod ladder is exhausted -> the terminal rung: mark FAILED + escalate to the parent.
        if runtime_failure:
            return _fail_and_escalate(
                node_address,
                binding,
                reason=runtime_failure.get("failure_class") or "runtime_failure",
                event="watchdog_runtime_failure",
                runtime_failure=runtime_failure,
            )
        return _fail_and_escalate(node_address, binding)

    # Within grace: PROD (gated on the idle-prompt string). A gate-closed pane is NOT prodded (a
    # send-keys nudge mid-tool-call corrupts the input line — §4.3 Precondition 1). A gate-closed
    # node does NOT advance the counter (we only count UNANSWERED PRODS, not un-prodded idleness).
    if not prod_precondition(node):
        return WatchdogAction(
            kind=NOOP, node=node_address,
            detail={"reason": "prod_gate_closed", "liveness_state": state},
        )

    # PROD: PERSIST the ladder advance (stale_check_count += 1) via the single-writer
    # watchdog_checkpoint so the NEXT poll reads a higher count and the ladder converges to FAILED.
    # Without this the counter never grows and an unresponsive leaf is prodded forever (the §3.5 bug).
    _checkpoint(node_address, binding, liveness_state="idle", last_progress_at=last_progress_at)

    return WatchdogAction(
        kind=PROD, node=node_address,
        detail={
            "reason": "idle_within_grace",
            "stale_check_count": stale_check_count + 1,
            "stale_grace_checks": stale_grace_checks,
            "keystroke": nonresponse_prod_keystroke(node),
        },
    )


def _checkpoint(
    node_address,
    binding,
    *,
    liveness_state,
    last_progress_at,
    last_evidence=None,
):
    """Persist a watchdog observation (the two-counter advance/reset) via the single-writer executor.

    Routes through ``executor.watchdog_checkpoint`` (the ONE writer): an ``idle`` observation
    INCREMENTS ``stale_check_count`` (the ladder rung toward FAILED), a ``working``/``waiting``
    observation RESETS it to 0. Edge-triggered — a steady-healthy poll appends nothing. Fenced on
    the live ``owner_token`` (a stale actor's observation cannot move the counter).
    """
    from . import executor  # local import: avoid a module-load cycle
    executor.watchdog_checkpoint(
        node_address,
        condition=("idle" if liveness_state == "idle" else "healthy"),
        liveness_state=liveness_state,
        last_progress_at=last_progress_at,
        last_evidence=last_evidence,
        expected_owner_token=binding.get("owner_token"),
    )


def _fail_and_escalate(
    node_address: str,
    binding: dict,
    *,
    reason: str = "watchdog_nonresponse",
    event: str = "watchdog_nonresponse",
    runtime_failure: Optional[dict] = None,
) -> WatchdogAction:
    """The FAILED closing action (v1 floor): mark running->failed via the REAL executor + escalate.

    Marks the leaf failed THROUGH the single-writer executor with event='watchdog_nonresponse' (so
    the run-ledger row is distinguishable from an agent-self-emitted FAILED — actor='harnessd' is the
    executor's single-writer stamp, and the reason rides the event/summary/delta). Then ESCALATES TO
    THE PARENT (the returned action carries target=parent_address): the parent coordinator re-claims at
    the stable address (WATCHDOG §4 L444/L489). v1 does NOT auto-respawn from harnessd — no fresh
    spawn/claim/resume row is emitted for the leaf (the deferred lease-recovery machine owns that).
    """
    from . import executor  # local import: avoid a module-load cycle (executor imports nothing here)

    parent_address = binding.get("parent_address")

    runtime_failure = dict(runtime_failure or {})
    failure_class = runtime_failure.get("failure_class")

    # Mark running -> failed through the REAL executor. event names the harness-imposed failure
    # class and the delta/summary carry the reason so the row is NOT conflated with an
    # agent-self-emitted FAILED. NOT a collapse
    # call (collapse journals the §3.6 signal_FAILED event); this is the watchdog-imposed,
    # non-response/runtime FAILED.
    binding_delta = {
        "terminal_signal": "FAILED",
        "terminal_note": reason,
        "in_flight_release": True,
    }
    death_delta = chokepoint.actor_death_accounting_delta(
        binding,
        event=event,
        reason=reason,
    )
    binding_delta.update(death_delta)
    if failure_class:
        binding_delta["failure_class"] = failure_class
    if runtime_failure:
        binding_delta["runtime_failure"] = runtime_failure
    result = executor.transition(
        node_address,
        expected_state=binding["state"],
        expected_generation=binding["generation"],
        expected_owner_token=binding.get("owner_token"),
        target_state="failed",
        binding_delta=binding_delta,
        event=event,
        summary=(
            "watchdog-imposed FAILED: idle-but-pane-warm leaf exhausted the prod ladder "
            f"(reason={reason}); the parent re-claims at the stable address "
            "(v1 does NOT auto-respawn from harnessd — §4.4 / §2.9 INCLUDE-item #5)"
        ),
    )
    # ROUTE THE RESULT (RR-3 — the third terminal write in this function tree; its two siblings,
    # collapse (watchdog-2) and escalate (F5), were already routed): an ABORTED running->failed
    # transition (a CAS state/generation miss racing the concurrent IPC writer thread, or a
    # validate abort) writes NO WAL row and leaves the binding running — reporting kind=FAILED
    # there is a phantom success in the F2 sense. Return NOOP so the next tick recomputes from
    # durable truth and retries; only a COMMITTED transition reports FAILED.
    if not result.ok:
        return WatchdogAction(
            kind=NOOP, node=node_address,
            detail={"reason": "fail_transition_aborted",
                    "errors": list(result.errors or [])},
        )
    if (
        death_delta.get("respawn_parked_at")
        and not binding.get("respawn_parked_at")
    ):
        chokepoint.journal_respawn_parked(
            node_address,
            getattr(result, "binding", None) or binding,
        )
    chokepoint.recover_terminal_notification(node_address, getattr(result, "binding", None))

    return WatchdogAction(
        kind=FAILED, node=node_address, target=parent_address,
        detail={
            "reason": reason,
            "failure_class": failure_class,
            "runtime_failure": runtime_failure or None,
            "parent_address": parent_address,
            "escalate_to_parent": True,
            "auto_respawn": False,  # v1: the parent acts; harnessd does NOT respawn.
        },
    )


# ===========================================================================
# Prod helpers — the gate (prompt-string match) + verify-new-turn (no blind trust).
# ===========================================================================

def prod_precondition(node) -> bool:
    """The prod gate (§4.3 Precondition 1): True iff the captured pane shows the IDLE input prompt.

    A send-keys prod can land mid-tool-call and corrupt the input line; the prompt-string gate is
    what stops a nudge interleaving with an in-flight tool call. Two-part match against the REAL
    captured pane (the ③ wire behind ``_capture_pane``):

      * the golden idle-prompt marker (``FORK_PROMPT`` — the '❯' input line, MEASURED on the
        pinned CC v2.1.152, fixture-pinned) must be PRESENT, and
      * the working marker (``_WORKING_MARKER`` — 'esc to interrupt', what CC shows while
        generating) must be ABSENT — CC renders the '❯' box even mid-generation, so the prompt
        char alone would open the gate on a busy pane.

    An empty/unreadable pane reads gate-CLOSED (the conservative no-prod-un-gated posture), and
    so does a pane showing a blocking DIALOG (``_DIALOG_MARKER`` — a nudge's Enter would press
    the highlighted dialog option; probed live on the trust dialog, which also renders '❯').
    """
    pane = _capture_pane(node)
    if not pane:
        return False
    if _WORKING_MARKER in pane:
        return False  # mid-generation/tool-call — never type into an in-flight turn (§4.3 P1)
    if any(marker in pane for marker in _DIALOG_MARKERS):
        return False  # a blocking dialog — Enter would CONFIRM the highlighted option
    return any(marker in pane for marker in PROMPT_MARKERS)  # E4: CC '❯' or Codex '›'


def confirm_prod_worked(node, jsonl_size_before) -> bool:
    """Verify-new-turn (§4.3 Precondition 3): True iff the transcript shows agent progress.

    send-keys is fire-and-forget (no ack); the watchdog confirms a prod "worked" ONLY by observing a
    NEW turn, never by assuming the keystroke landed. Byte growth alone is not enough: provider/API
    error rows such as Claude Code ``api_error`` / 529 Overloaded grow the JSONL but mean the agent
    did not consume the durable inbox pointer. An absent/unreadable transcript reads as no-progress
    (False) — no blind trust.
    """
    import json
    import os

    path = node.get("transcript_path") if isinstance(node, dict) else None
    if not path:
        return False
    try:
        size_now = os.stat(path).st_size
    except OSError:
        return False
    if size_now <= jsonl_size_before:
        return False

    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(max(0, int(jsonl_size_before or 0)))
            except (OSError, ValueError):
                fh.seek(0)
            tail = fh.read()
    except OSError:
        return False

    for raw in tail.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if runtime_failures.runtime_failure_from_transcript_event(row):
            continue
        if runtime_failures.transcript_event_is_agent_progress(row):
            return True
    return False


# ===========================================================================
# The ③-wake battery — coherent unconsumed-row snapshot + pointer payload.
# ===========================================================================

def unconsumed_inbox_notification(node, binding) -> Optional[dict]:
    """Return one coherent ③-wake snapshot for complete rows after the receipt.

    The notification metadata and eventual ack target MUST describe the same
    bytes. Returning the last complete-row offset from this scan prevents a row
    appended during send from being acknowledged by a notification that never
    counted it.

    ``count`` includes every complete unconsumed row. ``sender_counts`` keeps
    first-seen sender order and recognizes both canonical ``sender`` and
    compatibility ``from`` fields. ``should_wake`` preserves the existing wake
    table: barrier-mode child completion rows remain silent until a later
    wakeable row arrives.
    """
    inbox_path = _inbox_path(node)
    try:
        acked = max(0, int(binding.get("last_inbox_acked_offset", 0) or 0))
    except (TypeError, ValueError):
        acked = 0

    # Zero-knob wake table. Unknown/legacy event types default WAKE so an extension cannot silently
    # strand work. Only barrier-mode child completions are silent; their later barrier_complete row
    # wakes the parent once for the cohort.
    wake_table = {
        "message": True,
        "barrier_complete": True,
        "child_collapsed": None,
    }
    count = 0
    sender_counts: dict[str, int] = {}
    should_wake = False
    covered_offset = acked
    try:
        with inbox_path.open("rb") as handle:
            handle.seek(acked)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break
                covered_offset = handle.tell()
                if not raw.strip():
                    continue
                count += 1
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    row = {}
                sender = str(
                    (row.get("sender") or row.get("from") or "unknown")
                    if isinstance(row, dict)
                    else "unknown"
                )
                sender_counts[sender] = sender_counts.get(sender, 0) + 1
                event_type = row.get("type") if isinstance(row, dict) else None
                configured = wake_table.get(event_type, True)
                if configured is True:
                    should_wake = True
                if configured is None and not bool(row.get("barrier_mode")):
                    should_wake = True  # compatibility child_collapsed rows retain their old wake
    except OSError:
        return None
    if count == 0:
        return None
    return {
        "count": count,
        "sender_counts": tuple(sender_counts.items()),
        "covered_offset": covered_offset,
        "should_wake": should_wake,
    }


def inbox_has_unacked(node, binding) -> bool:
    """Compatibility boolean projection of the coherent ③-wake snapshot."""
    notification = unconsumed_inbox_notification(node, binding)
    return bool(notification and notification["should_wake"])


def wake_keystroke(node, notification: Optional[dict] = None) -> str:
    """The ③-wake send-keys payload: sender/count metadata plus a pointer.

    The content metadata is derived from complete unconsumed inbox rows; the
    message body remains in the durable row/artifact and is never stuffed into
    the keystroke.

    THE FILE IT NAMES (LT-10): the seat-qualified ``.inbox.<seat>.jsonl`` in the pane's own
    workspace (the F18 cwd) — the SAME file the kickoff pointer names. The §2.9/TRANSPORTS
    '<node>/.inbox.jsonl' wording predates the seat-qualified inbox: '<addr>#seat' is not a path
    segment, so the old pointer named a NONEXISTENT file — a resumed/necro'd incarnation that
    never saw the kickoff would `cat` nothing and report no-new-messages with the watermark
    already advanced (a delivered wake converted into a missed message).
    """
    from . import addressing

    notification = notification or unconsumed_inbox_notification(node, node)
    if not notification or not notification.get("should_wake"):
        raise ValueError("an inbox wake requires at least one wakeable unconsumed row")
    node_address = _node_address(node)
    seat = addressing.split_address(node_address)[1]
    count = int(notification["count"])
    noun = "message" if count == 1 else "messages"
    sender_parts = [
        f"{int(sender_count)} from {sender}"
        for sender, sender_count in notification["sender_counts"]
    ]
    return (
        f"{count} new {noun}: {', '.join(sender_parts)} — re-read only "
        f".inbox.{seat}.jsonl in your workspace and resume"
    )


def nonresponse_prod_keystroke(node) -> str:
    """Leaf liveness PROD text — not an inbox notification and not its retry budget."""
    return (
        f"turn-end contract for {_node_address(node)}: produce what you owe, "
        "explain, or escalate"
    )


def _inbox_path(node):
    """Resolve the per-SEAT wake inbox ``<nested-node-dir>/.inbox.<seat>.jsonl`` (``addressing.inbox_path``)
    — the same nested node dir as ``.signal.<seat>.json``, seat-qualified so the L5/L5+ pair don't share
    a wake surface."""
    from . import addressing

    root = ledger.RUNTIME_ROOT
    if root is None:
        raise RuntimeError(
            "inbox path is not configured: bind ledger.RUNTIME_ROOT (the runtime tree root where "
            "nodes/<nested-path>/.inbox.<seat>.jsonl lives)"
        )
    return addressing.inbox_path(_node_address(node), root)


# ===========================================================================
# The coordinator-death probe (§5.1 / §5.5).
# ===========================================================================

def check_coordinator_death(node, binding, ledger) -> WatchdogAction:
    """The coordinator process-death probe (§2.9 / §5.1): dead-pid + live children -> ESCALATE.

    Reads the run-ledger for a ``coordinator_died`` EVENT (an event, NOT a standing binding field —
    §5.1) OR ``state == 'dead'``. Then:

      * dead-pid + LIVE children -> RECOVERABLE ORPHAN -> ESCALATE (recover-vs-reap is DEFERRED to
        cluster ②'s policy, §5.2/§5.5; v1 NEVER reaps a coordinator with a live subtree — a dead
        coordinator over live descendants is recovered from the ledger, never blind-killed).
      * quiet pane-alive (no coordinator_died event, state not dead) + live children -> WAITING (the
        coordinator merely went quiet with live descendants — NOT an orphan; the pane_pid probe is
        the disambiguator between quiet-vs-dead).

    NOTE: ``ledger`` is the module passed by the caller (the §2.9 signature threads it explicitly);
    we read it for the coordinator_died event + the live-children roll-up so the probe keys off the
    REAL run-ledger, not a phantom field.
    """
    node_address = _node_address(node)

    # Is the coordinator process dead? Two legible signals (§5.1): a coordinator_died EVENT in the
    # run-ledger, OR a lifecycle state already at 'dead'.
    state = binding.get("state")
    died = state == "dead" or _has_coordinator_died_event(ledger, node_address)

    # Are there LIVE children below it? (the disambiguator that makes a dead coordinator an orphan).
    live_children = _has_live_children(ledger, node_address)

    if died and live_children:
        # WATCHDOG §3.4 STEP 0 on the ESCALATE branch: a PAUSED subtree gets no recovery action —
        # the recoverable-orphan ESCALATE is DEFERRED until resume (the verdict is recomputed from
        # durable state every tick; the next tick after resume escalates normally, nothing lost).
        if chokepoint.subtree_paused(node_address):
            return WatchdogAction(
                kind=NOOP, node=node_address,
                detail={"reason": "paused_subtree", "coordinator_dead": died, "live_children": True},
            )
        # A RECOVERABLE ORPHAN: dead process, live subtree -> ESCALATE (recover-vs-reap deferred).
        return WatchdogAction(
            kind=ESCALATE, node=node_address, target=binding.get("parent_address"),
            detail={
                "reason": "recoverable_orphan",
                "coordinator_dead": True,
                "live_children": True,
                "recover_vs_reap": "deferred",
            },
        )

    if not died and live_children:
        # Quiet but pane-alive with live children -> WAITING (not dead, not actionable).
        return WatchdogAction(
            kind=WAIT, node=node_address,
            detail={"reason": "quiet_alive_with_children", "coordinator_dead": False},
        )

    # Dead with no live children -> ①'s mechanical reconcile reap covers it (nothing to recover); a
    # live coordinator with no children is just a (childless) waiting/working node. Benign NOOP here.
    return WatchdogAction(
        kind=NOOP, node=node_address,
        detail={"reason": "no_live_subtree", "coordinator_dead": died},
    )


def _has_coordinator_died_event(ledger_mod, node_address: str) -> bool:
    """True iff the run-ledger carries a ``coordinator_died`` event for ``node_address`` (§5.1)."""
    for record in ledger_mod.load_wal():
        if record.get("node_address") == node_address and record.get("event") == "coordinator_died":
            return True
    return False


def _has_live_children(ledger_mod, node_address: str) -> bool:
    """True iff some binding names ``node_address`` as parent AND is in a live (non-terminal) state.

    Reuses the reconcile coordinator/leaf discriminator shape: a child is a binding whose
    ``parent_address`` is this node (the §3.1 denormalized reconcile-speed pointer), or whose address
    is a strict descendant path (the prefix-arithmetic fallback). A child is LIVE iff its lifecycle
    state is non-terminal (running/claimed/spawning/blocked/planned) — a child already done/failed/
    dead is not a live descendant that makes the parent a recoverable orphan.
    """
    from . import states

    this_path = node_address.split("#", 1)[0]
    for child_address, child in ledger_mod.all_nodes().items():
        if child_address == node_address:
            continue
        is_child = (
            child.get("parent_address") == node_address
            or child_address.split("#", 1)[0].startswith(this_path + "/")
        )
        if not is_child:
            continue
        if not states.is_terminal(child.get("state", "")):
            return True
    return False
