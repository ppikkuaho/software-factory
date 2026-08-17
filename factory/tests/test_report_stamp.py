"""LR-23 remedy (a) — the GATE EVIDENCE-STAMP (user-ruled 2026-06-12, option A: detection only).

Run-2 forensics (design/working-notes/LIVE-RUN-2026-06-11-FINDINGS.md §LR-23): the ws-3 builder's
accepted DONE collapsed at 05:14:19Z, yet report.md's last write is 05:14:30Z — ELEVEN SECONDS
AFTER the gate. The collapsed agent's pane stays alive (LT-4: no production path reaps it) and
the fence protects only the LEDGER, so the report the parent reads is NOT the report the gate
approved. Option (a) is evidence-only — the gate verdict becomes an auditable SNAPSHOT:

  1. AT THE ACCEPTED-DONE COLLAPSE — ``chokepoint.collapse(addr, "DONE", ...)`` stamps the
     sha256 + byte-size of the node's report.md AS THE GATE SAW IT into the collapse WAL row's
     ``binding_delta`` AND onto the binding (``report_sha256`` / ``report_bytes``). FAILED /
     DIED* / DEAD collapses are exempt (no gate approved anything); a missing report.md at a
     DONE collapse (the substrate paths drive collapse without a tree) -> NO stamp, NEVER an
     error.
  2. AT THE PROMOTE ACCEPT PATH — ``promote.promote`` compares the CURRENT report.md hash of
     the promoted node against its stamp; on mismatch it journals ONE ``report_drift`` row
     naming BOTH hashes and PROCEEDS (non-blocking — the operator decides; delivery is never
     refused on a drifted report).

No seal, no reap (options b/c are explicitly NOT ruled). This also produces the
post-collapse-behavior DATA the user wants before considering the stronger remedies.

BIAS TO REAL (Lesson 7): real bindings through the REAL ledger; a real report.md under the real
node dir; the collapse routes through the REAL chokepoint/executor; promote performs a REAL
filesystem copy-out. No mocks.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

import harnessd.addressing as addressing
import harnessd.clock as clock
import harnessd.fencing as fencing
import harnessd.fidelity_playback as fidelity_playback
import harnessd.ledger as ledger
import harnessd.spawn.chokepoint as chokepoint
import harnessd.watchdog as watchdog
from harnessd.promote import promote


# ===========================================================================
# Runtime fixture — same precedent as test_promote.py: the /runtime/ jail root is
# a DISTINCT subdir of tmp_path so a delivery destination (a tmp_path sibling) is
# genuinely OUTSIDE the jail.
# ===========================================================================

@pytest.fixture
def runtime(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = runtime_root
    try:
        yield runtime_root
    finally:
        ledger.RUNTIME_ROOT = previous


ADDR = "proj/widget#exec"
SUBAGENT = "subagent-lr23a-01"
SESSION = "sess-uuid-lr23a-0001"

GATED_REPORT = (
    "# report\n\nbuilt per brief; R-1 satisfied.\n\n"
    "## Drove and Watched\n\nDrove the recipient-visible claim.\n\n"
    "## Inferred\n\nSupporting checks passed.\n\n"
    "## Residual Uncertainty\n\nNone beyond fixture scope.\n\n"
    "## Inventory\n\nNone.\n"
)
DRIFTED_REPORT = "# report\n\nPOLISHED AFTER THE GATE — trace stanzas moved, citations dropped.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed(
    *,
    node_address=ADDR,
    state="running",
    generation=5,
    lease_epoch=2,
    extra=None,
):
    token = fencing.mint_owner_token(node_address, SUBAGENT, SESSION, lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": None,
        "level": "L5",
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": None,
        "terminal_signal": None,
        "tmux_target": "harness:" + node_address,
    }
    if extra:
        rec.update(extra)
    ledger.write_binding({node_address: copy.deepcopy(rec)}, _lock_held=True)
    return token


def _read(node=ADDR):
    return ledger.read_binding(node)


def _write_report(runtime, node_address, text):
    node_dir = addressing.node_dir(node_address, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "report.md").write_text(text, encoding="utf-8")
    return node_dir / "report.md"


def _write_fidelity_judgment(runtime, node_address):
    node_dir = addressing.node_dir(node_address, runtime)
    brief = node_dir / "client-brief"
    evidence = node_dir / "evidence"
    brief.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "fidelity.txt").write_text(
        "recipient-visible playback observed\n",
        encoding="utf-8",
    )
    (brief / "intent-spec.md").write_text(
        """# Intent Spec
## Outcomes
| Outcome ID | Outcome |
|---|---|
| O-001 | The recipient receives the widget. |
## Requirements
| ID | Requirement | Tag | MNF | Reflect-back status |
|---|---|---|---|---|
| R-001 | Deliver the widget | decided | YES | confirmed |
""",
        encoding="utf-8",
    )
    (brief / "fidelity-judgment.md").write_text(
        """# Fidelity Judgment
Preliminary Verdict: accept
## Outcome Playback
| Outcome ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| O-001 | Opened the widget | Widget was recipient-visible | evidence/fidelity.txt | accept |
## MNF Playback
| MNF ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| R-001 | Exercised failure path | Safe refusal | evidence/fidelity.txt | accept |
""",
        encoding="utf-8",
    )
    posted = fidelity_playback.create_question(node_address)
    assert posted.get("ok"), posted
    answered = fidelity_playback.answer_question(
        node_address,
        question_id=posted["question_id"],
        decision="confirm",
        note="Owner confirms this report-stamp playback.",
    )
    assert answered.get("ok"), answered


def _write_signal(runtime, node_address, *, signal, owner_token):
    p = addressing.signal_path(node_address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "signal": signal, "ts": clock.now_utc(), "owner_token": owner_token, "evidence": {},
    }))


def _node_from(binding):
    return {
        "node_address": binding["node_address"],
        "transcript_path": binding.get("transcript_path"),
        "tmux_target": binding.get("tmux_target", "harness:t"),
    }


def _wal_rows(node_address=ADDR, event=None):
    rows = [r for r in ledger.load_wal() if r.get("node_address") == node_address]
    if event is not None:
        rows = [r for r in rows if r.get("event") == event]
    return rows


def _accept_signal(node_address):
    return {
        "decision": "accept",
        "level": "L1",
        "node_address": node_address,
        "acceptance_ref": "client-brief/intent-spec.md",
        "note": "intent-fidelity accept (Stage 5)",
    }


# ===========================================================================
# 1. THE STAMP AT THE ACCEPTED-DONE COLLAPSE (watchdog path — the gate runs,
#    then the collapse stamps what the gate just approved).
# ===========================================================================

def test_done_collapse_stamps_report_hash_onto_binding_and_wal_row(runtime):
    """The accepted-DONE collapse stamps report_sha256 + report_bytes of report.md AS THE GATE
    SAW IT — onto the binding AND into the collapse WAL row's binding_delta (LR-23 option a)."""
    token = _seed()
    _write_report(runtime, ADDR, GATED_REPORT)
    _write_signal(runtime, ADDR, signal="DONE", owner_token=token)

    action = watchdog.check_leaf(_node_from(_read()), _read(), now=clock.now_utc())
    assert getattr(action, "kind", "") == "COLLAPSE", (
        f"fixture sanity: the fenced DONE must collapse (got {action!r})"
    )

    after = _read()
    assert after["state"] == "done"
    assert after.get("report_sha256") == _sha(GATED_REPORT), (
        "the DONE collapse must stamp the sha256 of the report the gate approved onto the binding"
    )
    assert after.get("report_bytes") == len(GATED_REPORT.encode("utf-8")), (
        "the DONE collapse must stamp the byte-size of the gated report onto the binding"
    )

    rows = _wal_rows(event="signal_DONE")
    assert rows, "the §3.6 signal_DONE collapse row must exist"
    delta = rows[-1].get("binding_delta") or {}
    assert delta.get("report_sha256") == _sha(GATED_REPORT), (
        "the collapse WAL row's binding_delta must carry the gate-time report_sha256 "
        "(the durable audit fact reconcile/audit reads)"
    )
    assert delta.get("report_bytes") == len(GATED_REPORT.encode("utf-8"))


def test_failed_collapse_does_not_stamp(runtime):
    """FAILED collapses are exempt: no gate approved anything — no stamp lands even when a
    report.md happens to exist on disk."""
    token = _seed()
    _write_report(runtime, ADDR, GATED_REPORT)
    _write_signal(runtime, ADDR, signal="FAILED", owner_token=token)

    action = watchdog.check_leaf(_node_from(_read()), _read(), now=clock.now_utc())
    assert getattr(action, "kind", "") == "COLLAPSE"

    after = _read()
    assert after["state"] == "failed"
    assert "report_sha256" not in after and "report_bytes" not in after, (
        "a FAILED collapse must NOT stamp — only the accepted DONE is a gate verdict"
    )
    rows = _wal_rows(event="signal_FAILED")
    assert rows
    delta = rows[-1].get("binding_delta") or {}
    assert "report_sha256" not in delta and "report_bytes" not in delta


def test_done_collapse_without_report_is_no_stamp_never_an_error(runtime):
    """A DONE collapse with NO report.md on disk (the substrate paths drive collapse without a
    tree) collapses cleanly with NO stamp — a missing report is never a collapse error."""
    token = _seed()
    # No node dir, no report.md — the direct substrate collapse (test_collapse_result.py shape).
    result = chokepoint.collapse(ADDR, "DONE", expected_owner_token=token)
    assert result is not None and getattr(result, "ok", False) is True, (
        "a missing report.md must never fail the collapse (stamps are best-effort facts)"
    )
    after = _read()
    assert after["state"] == "done"
    assert "report_sha256" not in after and "report_bytes" not in after


# ===========================================================================
# 2. THE DRIFT CHECK AT THE PROMOTE ACCEPT PATH — journal ONE report_drift row
#    naming both hashes, then PROCEED (non-blocking; the operator decides).
# ===========================================================================

def _seed_done_for_promote(runtime, tmp_path, *, stamp=None):
    """A done/completed node with a binding-stamped filesystem destination (outside the jail)."""
    dest = tmp_path / "delivery-out" / "widget"
    extra = {
        "level": "L1",
        "deliverable_state": "completed",
        "delivery_destination": str(dest),
        "delivery_kind": "filesystem-path",
    }
    if stamp is not None:
        extra.update(stamp)
    _seed(state="done", extra=extra)
    return dest


def test_promote_accept_journals_one_drift_row_and_proceeds(runtime, tmp_path):
    """THE LR-23 SCENARIO END-TO-END: collapse stamps the gated report; the still-alive agent
    edits report.md AFTER the accepted DONE; the promote accept detects the drift, journals ONE
    report_drift row naming BOTH hashes, and STILL DELIVERS (non-blocking)."""
    token = _seed(extra={
        "level": "L1",
        "deliverable_state": "completed",
        "delivery_destination": str(tmp_path / "delivery-out" / "widget"),
        "delivery_kind": "filesystem-path",
    })
    _write_report(runtime, ADDR, GATED_REPORT)
    result = chokepoint.collapse(ADDR, "DONE", expected_owner_token=token)
    assert result is not None and result.ok, "fixture sanity: the DONE collapse must commit"
    assert _read().get("report_sha256") == _sha(GATED_REPORT)

    # The post-collapse mutation (Run-2 ws-3: report.md edited 11s AFTER the accepted DONE).
    _write_report(runtime, ADDR, DRIFTED_REPORT)
    _write_fidelity_judgment(runtime, ADDR)

    outcome = promote(ADDR, accept_signal=_accept_signal(ADDR))
    assert outcome.ok and outcome.delivered, (
        "drift detection is NON-BLOCKING: the promote must still deliver (the operator decides)"
    )
    dest = tmp_path / "delivery-out" / "widget"
    assert (dest / "report.md").read_text(encoding="utf-8") == DRIFTED_REPORT, (
        "delivery proceeds with the current bytes — option (a) detects, never seals"
    )

    rows = _wal_rows(event="report_drift")
    assert len(rows) == 1, f"exactly ONE report_drift row must land (got {len(rows)})"
    row = rows[0]
    delta = row.get("binding_delta") or {}
    assert delta.get("report_sha256_at_gate") == _sha(GATED_REPORT), (
        "the drift row must name the gate-time hash"
    )
    assert delta.get("report_sha256_at_promote") == _sha(DRIFTED_REPORT), (
        "the drift row must name the current (promote-time) hash"
    )
    assert _sha(GATED_REPORT) in (row.get("summary") or "") and _sha(DRIFTED_REPORT) in (
        row.get("summary") or ""
    ), "the summary must name BOTH hashes (the L1/operator-readable audit line)"


def test_promote_accept_with_matching_report_journals_nothing(runtime, tmp_path):
    """An undrifted report (current hash == stamp) journals NO drift row."""
    _seed_done_for_promote(
        runtime, tmp_path,
        stamp={"report_sha256": _sha(GATED_REPORT),
               "report_bytes": len(GATED_REPORT.encode("utf-8"))},
    )
    _write_report(runtime, ADDR, GATED_REPORT)
    _write_fidelity_judgment(runtime, ADDR)

    outcome = promote(ADDR, accept_signal=_accept_signal(ADDR))
    assert outcome.ok and outcome.delivered
    assert _wal_rows(event="report_drift") == [], (
        "a matching hash is the steady state — no drift row"
    )


def test_promote_accept_without_stamp_journals_nothing(runtime, tmp_path):
    """A binding with NO stamp (a pre-stamp-era node, or a FAILED-collapsed one) has nothing to
    compare — best-effort means silent skip, never a phantom drift row."""
    _seed_done_for_promote(runtime, tmp_path, stamp=None)
    _write_report(runtime, ADDR, GATED_REPORT)
    _write_fidelity_judgment(runtime, ADDR)

    outcome = promote(ADDR, accept_signal=_accept_signal(ADDR))
    assert outcome.ok and outcome.delivered
    assert _wal_rows(event="report_drift") == []


def test_promote_accept_with_vanished_report_journals_drift_and_proceeds(runtime, tmp_path):
    """A stamped report that has DISAPPEARED by promote time is the worst drift — journaled
    (current hash None) and the promote still proceeds (non-blocking)."""
    _seed_done_for_promote(
        runtime, tmp_path,
        stamp={"report_sha256": _sha(GATED_REPORT),
               "report_bytes": len(GATED_REPORT.encode("utf-8"))},
    )
    # The node dir exists (promote needs a source tree) but report.md was never written / removed.
    node_dir = addressing.node_dir(ADDR, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "artifact.txt").write_text("deliverable bytes\n", encoding="utf-8")
    _write_fidelity_judgment(runtime, ADDR)

    outcome = promote(ADDR, accept_signal=_accept_signal(ADDR))
    assert outcome.ok and outcome.delivered, "a vanished report must not block delivery"

    rows = _wal_rows(event="report_drift")
    assert len(rows) == 1
    delta = rows[0].get("binding_delta") or {}
    assert delta.get("report_sha256_at_gate") == _sha(GATED_REPORT)
    assert delta.get("report_sha256_at_promote") is None, (
        "an absent report at promote time records current hash None (the fact, not an error)"
    )
