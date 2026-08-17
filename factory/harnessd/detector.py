"""detector.liveness — the thin liveness floor (Increment 6).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.8 (the FROZEN detector.py interface):
        liveness(node_address) -> Liveness(state, last_progress_at: str | None)
        state in working | waiting | idle | dead   (+ explicit 'unknown' fail-loud verdict)
  - IMPLEMENTATION-PLAN Increment-6 Done-test (L711-719): the five verdict cases + the
    false-idle hazard + transcript_path-absent fails loud.
  - IMPLEMENTATION-PLAN §4.1 "detector verdicts (fully mockable)" (L559-571).
  - WATCHDOG §2.4 / §3.3 + config.W / config.SUSPICION_WINDOWS (the W window; the
    working-vs-waiting-vs-idle boundary; the false-idle hazard).

THE v1 FLOOR (fuses ONLY jsonl_progress + pane_alive):
    pane_dead == 1 OR pane gone                                   -> dead   (wins; checked first)
    grew within W                                                 -> working
    flat but WITHIN the W window  (FALSE-IDLE HAZARD)             -> working
    flat BEYOND W + pane warm + LEGIT reason                      -> waiting
        (legit reason = terminal_signal == ESCALATED, OR a coordinator with a
         live-descendant roll-up — the roll-up is not wired in the v1 floor)
    flat BEYOND W + pane warm + NO reason                         -> idle   (the only actionable flat)

FAIL-LOUD: a binding with NO transcript_path makes the floor RAISE (MissingTranscriptPath
propagates from jsonl_progress) — NEVER a silent dead/idle. (The §2.8 contract also permits an
explicit 'unknown' verdict; we fail loud by raising, the louder of the two sanctioned outcomes.)

THE TMUX SEAM (§2.11): the detector reaches tmux ONLY through detector_signals.pane_alive (a
module-level function the tests monkeypatch). The detector calls it via the LIVE module
attribute (detector_signals.pane_alive), holding no rebindable local copy; the frozen test
patches detector_signals.pane_alive unconditionally (its `_patch_signals_everywhere`), so the
mock is always honored. See the Increment-6 builder report for how the seam (and the
coordinator-rollup fork) were wired.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config, clock, ledger, turn_state
from . import detector_signals

# THE SIGNAL SEAM: the detector calls the two floor readers THROUGH the detector_signals
# module object (detector_signals.jsonl_progress / .pane_alive) — NOT through a top-level
# `from ... import name` copy. Reading the live module attribute means a test that
# monkeypatches detector_signals.jsonl_progress is always honored, and the detector holds
# NO rebindable local copy that could leak a stale patch across tests. (The frozen test's
# _patch_signals_everywhere patches detector_signals.* unconditionally and only also patches
# detector.* when the name exists here — which it deliberately does not.)


# The legit-reason terminal signal that flips a flat-beyond-W warm pane to `waiting`
# instead of `idle` (an agent that ESCALATED holds its slot waiting for an answer; it
# is NOT idle — WATCHDOG §2.4 / DAEMON §3.6). v1 floor recognizes this single reason
# on the leaf leg; the coordinator live-descendant roll-up is the deferred second leg.
_ESCALATED = "ESCALATED"


@dataclass(frozen=True)
class Liveness:
    """The §2.8 liveness verdict.

    state in working | waiting | idle | dead, plus an explicit 'unknown' reserved for the
    sanctioned fail-loud return (this floor raises instead, but the field domain includes it).
    last_progress_at is surfaced from the binding so the watchdog's W(state) math has the
    instant it needs without a second ledger read.
    """

    state: str
    last_progress_at: str | None


def _w_window(binding) -> int:
    """The W(state) suspicion window (seconds) for this node — keyed by TASK TYPE (WATCHDOG §3.3).

    The window is keyed on the **task-type / suspicion-window vocabulary** (`working` /
    `waiting_on_child` / `writing_final`, `config.SUSPICION_WINDOWS`), which WATCHDOG §3.3 says
    "the spawning level sets at spawn time" — NOT the canonical 4-value `liveness_state` enum
    (working|waiting|idle|dead), whose only overlapping token is `working`. So this reads the
    dedicated `suspicion_window_key` binding field the spawn step populates.

    DEFERRED (named gap, FORK-W-KEYING): the spawn step that POPULATES `suspicion_window_key`
    lands in Increment 10 (the chokepoint). Until then the field is absent and this floors to the
    `working` window (120s) — the TIGHTEST, so it errs toward earlier suspicion (safe), never
    toward masking a stalled node. An unknown key also floors to `working` rather than raising.
    The longer windows become reachable the moment the chokepoint sets the field — no detector
    change needed. (Recorded in FORK-DECISIONS.md.)
    """
    key = binding.get("suspicion_window_key") or "working"
    return config.SUSPICION_WINDOWS.get(key, config.SUSPICION_WINDOWS["working"])


def _within_w(last_progress_at: str | None, window: int) -> bool:
    """True iff last_progress_at is RECENT — i.e. less than W seconds in the past (within W).

    Routes through the canonical clock (clock.age_seconds) so the freshness comparison is
    offset-invariant. A None/absent last_progress_at cannot be within W (no proof of recent
    progress) -> False, so the floor does not read a stamp-less binding as spuriously working.

    Boundary is spec-exact (WATCHDOG §3.3): overdue iff `age > W`, so within-W iff `age <= W`
    (at EXACTLY age == W the node is still working, not yet overdue).
    """
    if not last_progress_at:
        return False
    return clock.age_seconds(last_progress_at) <= window


def _has_legit_waiting_reason(node_address: str, binding) -> bool:
    """Does a flat-beyond-W warm node have a LEGIT reason to read `waiting` not `idle`? (§2.8).

    v1 floor reason: the live, fenced terminal_signal == ESCALATED (an agent parked waiting for
    an answer-round-trip — it holds its slot, never collapses). We read it through the FENCED
    reader so a stale prior-incarnation leftover never manufactures a phantom waiting-reason;
    we fall back to the binding's own terminal_signal field when the reader yields nothing (the
    field is the spawn<->detector contract carrier the seeded tests drive).

    DEFERRED (the second legit reason): a COORDINATOR with a live-descendant roll-up also reads
    waiting. The v1 floor has no live-descendant signal wired (that roll-up lands with reconcile/
    watchdog in later increments), so this leg is intentionally a no-op hook here — documented,
    not silently dropped. See the Increment-6 builder report (FORK note).
    """
    # Prefer the fenced on-disk signal (the producer side); a stale token yields None there.
    # Narrow the except to the EXPECTED unbound-RUNTIME_ROOT case only (RuntimeError): when the
    # runtime root isn't bound we fall back to the binding's terminal_signal field. A corrupt
    # .signal.json is CONTAINED inside the reader itself (RR-2: read_terminal_signal journals
    # ``signal_artifact_invalid`` + quarantines the artifact and returns None — agent-written
    # bytes must never crash the daemon's poll loop into a relaunch crash-loop); the binding's
    # own terminal_signal fallback below still carries an escalated node's waiting reason, so
    # the contained rejection does not silently degrade it to `idle`.
    node = {"node_address": node_address,
            "transcript_path": binding.get("transcript_path"),
            "tmux_target": binding.get("tmux_target")}
    try:
        sig = detector_signals.read_terminal_signal(node, binding)
    except RuntimeError:
        sig = None  # runtime root unbound -> fall back to the binding field below
    escalated = (sig is not None and sig.get("signal") == _ESCALATED)
    if not escalated:
        # Fall back to the binding's own terminal_signal field (the seeded contract carrier).
        escalated = binding.get("terminal_signal") == _ESCALATED
    if not escalated:
        return False

    # SM-4: an ANSWERED escalation is no longer a legit waiting reason. The stamp is never
    # cleared in v1 (the answer rides it), so without this expiry a node that ever escalated
    # could never again read `idle` — the idle->prod->FAILED ladder was permanently disabled
    # post-answer. A NEW question (a fresh harness-observed artifact identity or a re-journaled
    # terminal_signal_at) re-arms the waiting reason.
    sig_artifact_seen_at = sig.get("_signal_artifact_seen_at") if sig is not None else None
    if detector_signals.escalation_answered(binding, sig_artifact_seen_at):
        return False
    return True


def _ledger_waiting_reason(node_address: str, binding: dict) -> bool:
    """The hook-era complete wait predicate, derived only from already-recorded binding state."""
    if ledger.RUNTIME_ROOT is None:
        return _has_legit_waiting_reason(node_address, binding)
    try:
        return bool(
            turn_state.ledger_wait_reasons(
                node_address,
                binding,
                runtime_root=ledger.RUNTIME_ROOT,
            )
        )
    except Exception:  # noqa: BLE001 - the legacy ESCALATED predicate remains the safe floor
        return _has_legit_waiting_reason(node_address, binding)


def _evidence_liveness(
    node_address: str,
    binding: dict,
    node: dict,
    *,
    pane_result: tuple[bool, int | None] | None = None,
    progress_result: tuple[bool, str | None] | None = None,
) -> Liveness:
    """The pre-hook evidence floor, retained only for explicit fallback cases."""
    last_progress_at = binding.get("last_progress_at")
    grew, _mtime_iso = (
        progress_result
        if progress_result is not None
        else detector_signals.jsonl_progress(node)
    )
    alive, _pane_pid = (
        pane_result if pane_result is not None else detector_signals.pane_alive(node)
    )
    if not alive:
        return Liveness(state="dead", last_progress_at=last_progress_at)
    if grew:
        return Liveness(state="working", last_progress_at=last_progress_at)
    if _within_w(last_progress_at, _w_window(binding)):
        return Liveness(state="working", last_progress_at=last_progress_at)
    if detector_signals.pane_activity(node):
        return Liveness(state="working", last_progress_at=last_progress_at)
    if _ledger_waiting_reason(node_address, binding):
        return Liveness(state="waiting", last_progress_at=last_progress_at)
    return Liveness(state="idle", last_progress_at=last_progress_at)


def _hook_liveness(node_address: str, binding: dict, node: dict) -> Liveness | None:
    """Return a hook-primary verdict, or None when this profile must use the evidence floor."""
    profile = binding.get("turn_hook_profile")
    if profile not in {turn_state.CLAUDE_FULL_EDGES, turn_state.CODEX_TURN_END_ONLY}:
        return None

    # Process death remains physics and always wins, even over a last-written running hook state.
    pane_result = detector_signals.pane_alive(node)
    if not pane_result[0]:
        return Liveness(state="dead", last_progress_at=binding.get("last_progress_at"))
    if binding.get("turn_hook_health") == "degraded":
        return _evidence_liveness(
            node_address,
            binding,
            node,
            pane_result=pane_result,
        )
    if ledger.RUNTIME_ROOT is None:
        return _evidence_liveness(
            node_address,
            binding,
            node,
            pane_result=pane_result,
        )

    observation = turn_state.read_current(
        node_address,
        binding,
        runtime_root=ledger.RUNTIME_ROOT,
    )
    if observation.status != "valid":
        return _evidence_liveness(
            node_address,
            binding,
            node,
            pane_result=pane_result,
        )
    payload = observation.payload or {}
    state = payload.get("state")

    if profile == turn_state.CLAUDE_FULL_EDGES:
        if state == turn_state.TURN_RUNNING:
            return Liveness(state="working", last_progress_at=binding.get("last_progress_at"))
        if state == turn_state.WAITING_ON_HUMAN:
            return Liveness(state="waiting", last_progress_at=binding.get("last_progress_at"))
        if state == turn_state.TOOL_IN_FLIGHT:
            updated_at = payload.get("updated_at")
            if updated_at and clock.age_seconds(updated_at) <= _w_window(binding):
                return Liveness(state="working", last_progress_at=binding.get("last_progress_at"))
            # A tool edge that has remained open past W is the explicit hung-tool wedge fallback.
            return _evidence_liveness(
                node_address,
                binding,
                node,
                pane_result=pane_result,
            )
        if state == turn_state.TURN_ENDED:
            return Liveness(
                state=(
                    "waiting"
                    if _ledger_waiting_reason(node_address, binding)
                    else "idle"
                ),
                last_progress_at=binding.get("last_progress_at"),
            )
        return _evidence_liveness(
            node_address,
            binding,
            node,
            pane_result=pane_result,
        )

    # Codex's legacy notify reports only the truthful end edge. A visible active pane or transcript
    # write strictly newer than that edge supplies the intentionally absent next-turn start edge.
    if state == turn_state.TURN_ENDED:
        if detector_signals.pane_activity(node):
            return Liveness(state="working", last_progress_at=binding.get("last_progress_at"))
        progress = detector_signals.jsonl_progress(node)
        grew, mtime_iso = progress
        if grew and mtime_iso and payload.get("updated_at"):
            try:
                if clock.parse_iso(mtime_iso) > clock.parse_iso(payload["updated_at"]):
                    return Liveness(
                        state="working",
                        last_progress_at=binding.get("last_progress_at"),
                    )
            except (TypeError, ValueError):
                pass
        return Liveness(
            state=(
                "waiting"
                if _ledger_waiting_reason(node_address, binding)
                else "idle"
            ),
            last_progress_at=binding.get("last_progress_at"),
        )
    return _evidence_liveness(
        node_address,
        binding,
        node,
        pane_result=pane_result,
    )


def liveness(node_address: str) -> Liveness:
    """Fuse jsonl_progress + pane_alive into a working|waiting|idle|dead verdict (§2.8 floor).

    Resolution order (each step's precedence is load-bearing):
      1. Resolve the binding (ledger.read_binding). Absent binding -> raise (no such node).
      2. jsonl_progress(node) — this is ALSO the fail-loud gate: a missing transcript_path
         raises MissingTranscriptPath here, which propagates (NEVER a silent dead/idle).
      3. pane_alive(node): pane_dead / pane gone -> `dead` WINS over every JSONL signal.
      4. grew -> `working`.
      5. flat but WITHIN W -> `working` (the FALSE-IDLE HAZARD: W must ELAPSE before flat
         can read idle/waiting; a warm pane in a long quiet model turn is still working).
      6. flat BEYOND W + warm pane + LEGIT reason -> `waiting`.
      7. flat BEYOND W + warm pane + NO reason -> `idle` (the only actionable flat case).
    """
    binding = ledger.read_binding(node_address)
    if binding is None:
        raise KeyError(f"no binding for node_address {node_address!r}")

    last_progress_at = binding.get("last_progress_at")

    # The node shape the signal readers operate on (binding carries the same fields).
    node = {
        "node_address": node_address,
        "transcript_path": binding.get("transcript_path"),
        "tmux_target": binding.get("tmux_target"),
    }

    hook_verdict = _hook_liveness(node_address, binding, node)
    if hook_verdict is not None:
        return hook_verdict

    # (2) FAIL-LOUD gate: jsonl_progress raises MissingTranscriptPath on a no-transcript binding.
    #     Calling it FIRST means a contract violation cannot be masked by a (possibly mocked)
    #     pane reading — the violation surfaces before any verdict is formed.
    grew, _mtime_iso = detector_signals.jsonl_progress(node)

    # (3) DEAD wins: a dead/gone pane is dead regardless of the JSONL growth signal.
    alive, _pane_pid = detector_signals.pane_alive(node)
    if not alive:
        return Liveness(state="dead", last_progress_at=last_progress_at)

    # (4) Grew -> working.
    if grew:
        return Liveness(state="working", last_progress_at=last_progress_at)

    # (5) FALSE-IDLE HAZARD: flat but still WITHIN W reads working, not idle.
    if _within_w(last_progress_at, _w_window(binding)):
        return Liveness(state="working", last_progress_at=last_progress_at)

    # (5b — LR-17) Flat BEYOND W but the pane CAPTURE shows the mid-turn marker -> working.
    # A running Task SUBAGENT (the L1 grilling dispatch its role doc prescribes) or a long quiet
    # model turn leaves the MAIN transcript flat while the pane renders 'esc to interrupt'; the
    # flat signal alone must not out-vote the pane's own in-flight indicator (Run-2 false-idle:
    # the ladder killed a healthy, spec-conformant L1 for dispatching its intake session).
    if detector_signals.pane_activity(node):
        return Liveness(state="working", last_progress_at=last_progress_at)

    # (6) Flat BEYOND W + warm pane + LEGIT reason -> waiting (NOT idle).
    if _has_legit_waiting_reason(node_address, binding):
        return Liveness(state="waiting", last_progress_at=last_progress_at)

    # (7) Flat BEYOND W + warm pane + NO reason -> idle (the only actionable flat case).
    return Liveness(state="idle", last_progress_at=last_progress_at)
