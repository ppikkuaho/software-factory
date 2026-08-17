"""The two 2026-07-16 gate-surface fixes (root-caused from the r6 + delivered-run evidence).

1. Harness-owned derived aggregates (`*.aggregate.md`) are excluded from BOTH gate surfaces:
   the return-contract trace scan and the candidate-artifact drift manifest. In r6
   (greenfield-trailmark, 2026-06-19) the daemon's append-relay folded a descendant log's
   prose trace placeholder into `log.aggregate.md` (MALFORMED-TRACE) and kept rewriting that
   file between submit and review (CANDIDATE-ARTIFACT-DRIFT) — livelocking a finished,
   reviewer-ACCEPTED product. The harness must never race its own gate.

2. The gate-verdict token parser tolerates markdown emphasis ("**Verdict:** ACCEPT"): the
   strict literal demanded a typography coin-flip that cost 6 MISSING-GATE-VERDICT
   refuse-retry cycles in the one delivered run.
"""

from __future__ import annotations

from pathlib import Path

from harnessd import return_contract, review_dispatch


MALFORMED_STANZA = "Some prose.\n<!-- trace: {...} -->\nMore prose.\n"


# ---------------------------------------------------------------------------------------
# 1a. Trace scan skips harness-owned aggregates.
# ---------------------------------------------------------------------------------------

def test_trace_scan_skips_harness_aggregates(tmp_path):
    """A malformed stanza inside log.aggregate.md / status.aggregate.md yields NO defect —
    the daemon authors those files; the producer cannot stabilize them."""
    (tmp_path / "log.aggregate.md").write_text(MALFORMED_STANZA, encoding="utf-8")
    (tmp_path / "status.aggregate.md").write_text(MALFORMED_STANZA, encoding="utf-8")
    defects = return_contract._check_trace_stanzas("proj/widget#exec", tmp_path)
    assert defects == [], f"harness-owned aggregates must not be scanned; got {defects}"


def test_trace_scan_still_bites_on_return_evidence(tmp_path):
    """Control: the SAME malformed stanza in report.md (real return evidence) still defects —
    the exclusion is scoped to the harness's own files, not a blanket relaxation."""
    (tmp_path / "report.md").write_text(MALFORMED_STANZA, encoding="utf-8")
    defects = return_contract._check_trace_stanzas("proj/widget#exec", tmp_path)
    assert any("MALFORMED-TRACE" in d for d in defects), (
        f"a malformed stanza in report.md must still defect; got {defects}"
    )


# ---------------------------------------------------------------------------------------
# 1b. Candidate-drift manifest excludes harness-owned aggregates.
# ---------------------------------------------------------------------------------------

def test_candidate_manifest_excludes_aggregates():
    excluded = review_dispatch._candidate_artifact_excluded
    assert excluded(Path("log.aggregate.md")) is True
    assert excluded(Path("status.aggregate.md")) is True
    assert excluded(Path("sub/log.aggregate.md")) is True
    # Control: real product/report files stay inside the frozen identity.
    assert excluded(Path("report.md")) is False
    assert excluded(Path("logview/cli.py")) is False


# ---------------------------------------------------------------------------------------
# 2. Verdict-token tolerance.
# ---------------------------------------------------------------------------------------

def test_gate_verdict_accepts_markdown_wrapped_tokens():
    forms = [
        "VERDICT: ACCEPT",
        "**Verdict:** ACCEPT",
        "**VERDICT: BOUNCE**",
        "## Verdict: escalate",
        "Verdict — ACCEPT",
    ]
    for text in forms:
        match = return_contract._GATE_VERDICT.search(text)
        assert match, f"verdict form must parse: {text!r}"


def test_gate_verdict_still_requires_an_explicit_token():
    for text in (
        "The reviewer leaned toward acceptance overall.",
        "No verdict was reached in this round.",
        "ACCEPT",  # bare token with no VERDICT marker is not an explicit verdict line
    ):
        assert return_contract._GATE_VERDICT.search(text) is None, (
            f"non-verdict prose must not parse as a verdict: {text!r}"
        )


# ---------------------------------------------------------------------------------------
# 3. The gate-loop circuit breaker (r6-class livelock containment).
# ---------------------------------------------------------------------------------------

def _wal_row(node, defects, signal_id):
    return {
        "event": return_contract.EVENT,
        "node_address": node,
        "binding_delta": {"defects": defects, "signal_artifact_seen_at": signal_id},
    }


def _drive_refusal(monkeypatch, tmp_path, prior_rows, *, binding=None):
    """Drive journal_defects_once against a synthetic WAL; return (paused, journaled, l1_inbox)."""
    import harnessd.addressing as addressing
    import harnessd.ledger as ledger_mod

    node = "L1/trailmark#exec"
    paused = []
    journaled = []
    monkeypatch.setattr(ledger_mod, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(ledger_mod, "load_wal", lambda: list(prior_rows))
    # The run's L1 root — the breaker must route its escalation THROUGH L1 (2026-07-17 ruling).
    monkeypatch.setattr(
        ledger_mod, "all_nodes",
        lambda **kw: {"L1#exec": {"level": "L1", "state": "running"}},
    )
    l1_dir = addressing.node_dir("L1#exec", tmp_path)
    l1_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        return_contract.executor, "pause",
        lambda addr, **kw: paused.append(addr),
    )
    monkeypatch.setattr(
        return_contract.executor, "journal",
        lambda addr, **kw: journaled.append((addr, kw.get("event"))),
    )
    ok = return_contract.journal_defects_once(
        node,
        binding if binding is not None else {"state": "running", "lease_epoch": 2},
        "fresh-signal-identity",
        ["MALFORMED-TRACE: log.md in L1/trailmark#exec: unparseable field '...'"],
    )
    assert ok is True, "a fresh signal identity must journal"
    l1_inbox = addressing.inbox_path("L1#exec", tmp_path)
    l1_lines = l1_inbox.read_text(encoding="utf-8").splitlines() if l1_inbox.is_file() else []
    return paused, journaled, l1_lines


def test_circuit_breaker_fires_at_threshold(monkeypatch, tmp_path):
    """Three journaled refusals with the SAME defect signature on one node -> the node is
    PAUSED and a gate_loop_circuit_broken row lands. (r6 spun forever; the breaker bounds it.)"""
    node = "L1/trailmark#exec"
    prior = [
        _wal_row(node, ["MALFORMED-TRACE: a"], "s1"),
        _wal_row(node, ["MALFORMED-TRACE: b"], "s2"),
        _wal_row(node, ["MALFORMED-TRACE: c"], "s3"),
    ]
    paused, journaled, l1_lines = _drive_refusal(monkeypatch, tmp_path, prior)
    assert paused == [node], "the looping node must be paused at the threshold"
    assert (node, return_contract.CIRCUIT_BREAKER_EVENT) in journaled
    # The intervention routes THROUGH L1 (owner ruling 2026-07-17): a durable inbox line the
    # daemon's wake machinery delivers, so L1 owns the disposition — never a direct human page.
    assert any("gate_loop_circuit_broken" in l for l in l1_lines), (
        "the breaker must escalate to the L1 root inbox"
    )


def test_circuit_breaker_needs_identical_signature(monkeypatch, tmp_path):
    """Refusals with DIFFERENT defect signatures do not accumulate toward one breaker trip."""
    node = "L1/trailmark#exec"
    prior = [
        _wal_row(node, ["MALFORMED-TRACE: a"], "s1"),
        _wal_row(node, ["CANDIDATE-ARTIFACT-DRIFT: x"], "s2"),
        _wal_row(node, ["CANDIDATE-ARTIFACT-ADDED: y"], "s3"),
    ]
    paused, journaled, l1_lines = _drive_refusal(monkeypatch, tmp_path, prior)
    assert paused == [], "mixed signatures must not trip the breaker"
    assert all(ev != return_contract.CIRCUIT_BREAKER_EVENT for _, ev in journaled)
    assert not any("gate_loop_circuit_broken" in l for l in l1_lines)


def test_circuit_breaker_never_retrips_a_paused_node(monkeypatch, tmp_path):
    """A node already paused (breaker fired, or human pause) never re-trips the breaker."""
    node = "L1/trailmark#exec"
    prior = [
        _wal_row(node, ["MALFORMED-TRACE: a"], "s1"),
        _wal_row(node, ["MALFORMED-TRACE: b"], "s2"),
        _wal_row(node, ["MALFORMED-TRACE: c"], "s3"),
    ]
    paused, journaled, l1_lines = _drive_refusal(
        monkeypatch, tmp_path, prior,
        binding={"state": "running", "lease_epoch": 2, "paused_at": "2026-07-16T00:00:00Z"},
    )
    assert paused == []
    assert all(ev != return_contract.CIRCUIT_BREAKER_EVENT for _, ev in journaled)
    assert not any("gate_loop_circuit_broken" in l for l in l1_lines)
