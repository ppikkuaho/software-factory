"""Single executable authority for authored run-ledger event disposition.

Recovery, documentation tests, and the authored-vocabulary completeness check
all consume this table.  A replay disposition means applying the row's binding
effect is required to rebuild canonical state; every other disposition keeps
the row for its named non-binding consumer and must never merge its delta into
a recovered binding.

Disposition also has an incarnation axis: an authored replay event describes an
address that may since have been re-registered past the row's ``lease_epoch``
(:func:`epoch_fenced`).  Replay reaches that conclusion on its own — the
per-node watermark already sorts every pre-registration row below the seed — so
the predicate exists for the READ projection, which has no watermark arithmetic
to lean on.
"""

from __future__ import annotations

from collections.abc import Mapping


REPLAY = "replay"
RECOVERY_CONSUMER = "recovery_consumer"
EVIDENCE_CONSUMER = "evidence_consumer"
CONDITIONAL = "conditional"
NODE_LOCAL = "node_local"


def _classified(names: str, disposition: str) -> dict[str, str]:
    return {name: disposition for name in names.split()}


EVENT_CLASSIFICATION = {
    **_classified(
        """
        accepted_test_contract_home_recovered admission_blocked admission_released
        admission_updated brief_written claim collapse_dead contract_rebound
        coordination_handoff_marker_invalid coordination_handoff_submitted
        coordinator_completed coordinator_died delivered delivery_failed
        died_infrastructure died_methodology fidelity_playback_authority_bound
        fidelity_playback_authority_delegated
        fidelity_playback_commissioning_delegate_answered
        fidelity_playback_owner_answered fidelity_playback_question_posted
        gate_bounced gate_candidate_submitted gate_escalated gate_failed gate_passed
        gate_review_bounced gate_review_escalated gate_review_parent_resolved
        gate_review_passed heartbeat human_answer_posted inbox_acked
        intent_spec_revised message_answered message_marker_invalid message_recorded
        open_questions_withdrawn parent_answer_posted paused
        plan_alignment_decision_posted plan_alignment_marker_invalid
        plan_alignment_owner_question_answered
        plan_alignment_owner_questions_adopted plan_alignment_semantic_cell_failed
        plan_alignment_semantic_cell_pending plan_alignment_semantic_cell_ready
        release_claim release_lease resumed seat_respawn_streak_reset
        seat_stall_actioned seat_stall_recovered seat_stalled
        semantic_comparator_released signal_DONE signal_ESCALATED signal_FAILED
        spawn_contracts_receipted spawn_open spawn_running test_refresh_approved
        test_refresh_requested test_refresh_review_passed transition
        turn_exit_prod_delivered turn_hook_degraded turn_hook_recovered wake_cap
        wake_verify_pending watchdog_checkpoint watchdog_nonresponse
        watchdog_runtime_failure
        """,
        REPLAY,
    ),
    **_classified(
        "git_auto_merge_outcome reconcile_escalation return_contract_failed",
        RECOVERY_CONSUMER,
    ),
    **_classified(
        """
        boot_confirm_pending delivery_failed_escalation gate_escalation_nonconverging
        gate_loop_circuit_broken git_merged kickoff_append_failed kickoff_send_failed
        prod_send_failed report_drift run_freeze_acked run_freeze_notify_failed
        run_freeze_paused run_frozen seat_respawn_parked signal_artifact_invalid
        spawn_failed stale_return_ignored turn_exit_prod_send_failed
        turn_state_stale_ignored wake_hold_cleared wake_hold_engaged wake_send_failed
        watchdog_sweep_error
        """,
        EVIDENCE_CONSUMER,
    ),
    "ipc_request_failed": CONDITIONAL,
    "turn_state_observed": NODE_LOCAL,
}


def replay_changes_binding(event: Mapping[str, object]) -> bool:
    """Return true only for a canonical binding effect.

    Authored event names use the table. The operator repair transition permits
    a caller-supplied audit label, so an otherwise-unclassified row retains the
    established generic-transition rule: a strict one-generation CAS advance
    is a binding effect by record shape. A classified replay-neutral row always
    stays neutral regardless of incidental generation fields.
    """
    event_type = str(event.get("event") or "")
    disposition = EVENT_CLASSIFICATION.get(event_type)
    if disposition is not None:
        return disposition == REPLAY
    expected = event.get("expected_generation")
    generation = event.get("generation")
    return (
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation == expected + 1
    )


def epoch_fenced(binding: Mapping[str, object], event: Mapping[str, object]) -> bool:
    """Return true when the row belongs to an incarnation the binding already retired.

    ``lease_epoch`` is the per-address incarnation identity (DAEMON §8), and
    re-registration seeds the next epoch strictly above every epoch the address
    has worn (``chokepoint.reregister_identity_seed``).  A committed row minted
    below the live epoch is therefore true history of a superseded incarnation:
    it is kept forever and consumed as journal, but its ``binding_delta`` is not
    a canonical effect of the incarnation now holding the address.

    Generation cannot answer this: the same re-registration resets generation to
    0, so a retired row's pre-image can match the fresh registration by
    coincidence.  Epoch is the axis that separates them.

    Both epochs must be present integers for the fence to engage: a journal-only
    row carries no epoch, and an unepoched binding presents no incarnation to
    fence against.
    """
    row_epoch = event.get("lease_epoch")
    live_epoch = binding.get("lease_epoch")
    return (
        isinstance(row_epoch, int)
        and not isinstance(row_epoch, bool)
        and isinstance(live_epoch, int)
        and not isinstance(live_epoch, bool)
        and row_epoch < live_epoch
    )
