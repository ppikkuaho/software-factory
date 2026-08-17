"""base — the hexagonal RuntimeAdapter port (IMPLEMENTATION-PLAN §2.11; DAEMON §6.3; E31/E32).

``RuntimeAdapter`` is the runtime-NEUTRAL spawn contract: ``pin_and_open`` confirms the
configured model+runtime is PINNED before the child runs, opens the tmux actor, and records
the actual ``model_used`` (config = INTENT, model_used = FACT). The port injects ONLY the three
runtime-specific things (tool manifest, harness invocation, output format) over the neutral
contract; a level NEVER picks its own model/runtime (E31).

The result of a spawn is ``SpawnResult`` (the §2.13 dataclass, EXTENDED with the
``session_uuid`` / ``transcript_path`` the spawn<->detector contract producer records — the
detector stats ``transcript_path`` and reconcile matches ``session_uuid``). It is defined HERE,
the port module, so both fills and the chokepoint share the ONE result shape.

Spawn-failure classification (DAEMON §6.3): on a refused spawn the fill raises a typed failure
({auth_expired, model_unavailable, override_rejected, runtime_down}) — NO substitute model, NO
best-effort fallback. The taxonomy (``SpawnFailure`` / ``AuthExpired`` / ``ApiKeyForbidden``)
lives in ``oauth_guard`` (Increment 8); this module re-exports nothing — fills import it directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SpawnResult:
    """The outcome of a ``pin_and_open`` (§2.13, extended for the spawn<->detector contract).

    ``model_used`` is the ACTUAL model+runtime fact (always recorded, even on failure paths that
    still know it). ``role_variant`` / ``system_prompt_file`` / ``system_prompt_file_hash`` pin
    WHICH seat + WHICH shared prompt booted. ``session_uuid`` + ``transcript_path`` are the
    producer half of the spawn<->detector contract — the detector stats ``transcript_path`` (a
    ``<session-uuid>.jsonl`` file derived from ``session_uuid``); a null path breaks the contract.
    ``argv`` / ``env`` carry the assembled child command + the 4-var isolation env so a dry-run
    caller can inspect the assembly without a real exec.

    ``permission_posture`` journals the spawn's permission stance (SECURITY.md §4.3 — auditable
    the same way OAuth-only is): ``jailed-skip-permissions`` (the constraint-4 coupling),
    ``unjailed-prompting`` (the unjailed default), or ``unjailed-skip-permissions-override``
    (the USER-APPROVED supervised-smoke knob — see ``config.unjailed_skip_permissions_requested``).
    Default None so pre-existing fakes/fills that do not stamp it stay valid.

    ``codex_seat_id`` / ``codex_auth_version`` are Codex-only evidence fields. They identify the
    access-token-only worker home and the canonical auth generation it was seeded from, without
    recording token values.
    """

    ok: bool
    session_uuid: str | None
    model_used: str
    role_variant: str
    system_prompt_file: str
    system_prompt_file_hash: str
    tmux_target: str
    transcript_path: str | None
    failure_class: str | None = None
    argv: tuple[str, ...] = ()
    env: dict | None = None
    permission_posture: str | None = None
    containment_posture: dict | None = None
    codex_seat_id: str | None = None
    codex_auth_version: str | None = None
    codex_access_seconds_remaining: int | None = None
    launch_packet_file: str | None = None
    launch_packet_hash: str | None = None
    launch_surface_source_hash: str | None = None
    reference_map_file: str | None = None
    reference_map_hash: str | None = None
    reference_map_json_file: str | None = None
    launch_surface_version: str | None = None


class RuntimeAdapter(ABC):
    """The runtime-NEUTRAL spawn port (hexagonal). Concrete fills: ClaudeCode, Codex (stub).

    A fill MUST:
      * confirm the configured model+runtime is PINNED before the child runs (E32);
      * enforce the OAuth-only invariant BEFORE opening any tmux actor (§2.11);
      * on a refused spawn raise a typed ``SpawnFailure`` (no substitute, no best-effort);
      * ALWAYS record the actual ``model_used`` (config = intent, model_used = fact).
    """

    @staticmethod
    def interactive_prompt_signature(pane_text: str) -> str | None:
        """Return one measured interactive-chooser signature, else fail closed.

        Prompt vocabulary is runtime-owned because TUI shapes are pin-specific.  The neutral
        default deliberately recognizes nothing; a concrete adapter adds only fixture- or
        trace-grounded shapes.
        """
        return None

    @abstractmethod
    def pin_and_open(self, neutral_brief, level_config, tmux_target, env) -> SpawnResult:
        """Pin model+runtime, gate OAuth-only, open the tmux actor; return a ``SpawnResult``."""
        raise NotImplementedError
