"""Phase D2: one-winner atomic composition-gate finalization races."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from composition_gate.decision import DecisionError, prepare_decision
from conftest import Sandbox, seed_mrec_candidate, transcribe_engine_screen
from ht import cgate as root_cgate
from ht.commands.cgate import FINALIZATION_FORMAT


def _prepared_auto(sb: Sandbox) -> tuple[str, dict]:
    adjudication_ref = seed_mrec_candidate(sb, "tree#L1/node#1")
    created = sb.run(
        "mrec", "create",
        "--candidate-ref", "tree#L1/node#1",
        "--lane-verdict", "lane-pass",
        "--lane-adjudication-ref", adjudication_ref,
        "--scope-lane", "L1",
        role="harness",
    )
    assert created.returncode == 0, created.stderr
    screened = transcribe_engine_screen(sb, "MR-1")
    assert screened.returncode == 0, screened.stderr
    return "MR-1", {
        "format": FINALIZATION_FORMAT,
        "decision": prepare_decision(sb.root, "MR-1"),
        "stage2": None,
    }


def test_two_finalizers_same_preparation_have_exactly_one_winner(
    sandbox: Sandbox,
    monkeypatch,
):
    record_id, payload = _prepared_auto(sandbox)
    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    barrier = threading.Barrier(2)
    call_lock = threading.Lock()
    enforce_calls = 0
    real_enforce = root_cgate._enforce_and_commit

    def counted_enforce(*args, **kwargs):
        nonlocal enforce_calls
        with call_lock:
            enforce_calls += 1
        return real_enforce(*args, **kwargs)

    monkeypatch.setattr(root_cgate, "_enforce_and_commit", counted_enforce)

    def finalize():
        barrier.wait()
        try:
            return "won", root_cgate.finalize_prepared(sandbox.root, payload)
        except DecisionError as exc:
            return "lost", (exc.kind, exc.message)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _value: finalize(), range(2)))

    assert sorted(outcome[0] for outcome in outcomes) == ["lost", "won"]
    assert enforce_calls == 1
    assert sandbox.git("rev-list", "--count", f"{head_before}..HEAD").stdout.strip() == "1"
    assert [path.name for path in sorted((sandbox.root / "tier1/gate-reviews").glob("GR-*.json"))] == ["GR-1.json"]
    assert not list((sandbox.root / "tier1/ratification-queue").glob("RQ-*.json"))
    record = sandbox.load(f"tier1/merge-records/{record_id}.json")
    assert record["gate_verdict"]["review_ref"] == "GR-1"
    assert sandbox.git("status", "--short").stdout == ""
