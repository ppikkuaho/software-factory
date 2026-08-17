"""Phase D1: committed-snapshot decision preparation and dry-run CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
import zipfile

import pytest

from composition_gate import packet, stage2
from composition_gate.cli import (
    STANDALONE_DECIDE_ERROR,
    main as cgate_main,
)
from composition_gate.decision import DecisionError, prepare_decision, render_decision
from composition_gate.stage2 import GenerationResult
from conftest import (
    Sandbox,
    seed_mrec_candidate,
    seed_worked_node,
    transcribe_engine_screen,
)
from ht import jsonio
from ht.cgate import execute_decision, finalize_prepared
from ht import cgate as root_cgate
from ht.commands.cgate import FINALIZATION_FORMAT


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSITION_SOURCE = PROJECT_ROOT / "system/instruments/composition-gate"
ROOT_SYSTEM_SOURCE = PROJECT_ROOT / "system"
COMMON_KEYS = {
    "record_id",
    "route",
    "stage",
    "verdict",
    "note",
    "rules_fired",
    "screen_ref",
    "fingerprints",
}
SCREEN_REF_KEYS = {
    "output_ref",
    "log_ref",
    "log_sha256",
    "output_sha256",
    "computed",
    "head_commit",
    "head_tree",
    "config_hash",
    "engine_version",
}


def _create_pending(sb: Sandbox, lane: str) -> str:
    candidate_ref = f"tree#{lane}/node#1"
    adjudication_ref = seed_mrec_candidate(sb, candidate_ref)
    result = sb.run(
        "mrec",
        "create",
        "--candidate-ref",
        candidate_ref,
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        lane,
        role="harness",
    )
    assert result.returncode == 0, result.stderr
    return f"MR-{len(_committed_record_paths(sb))}"


def _committed_record_paths(sb: Sandbox) -> list[str]:
    result = sb.git("ls-tree", "-r", "--name-only", "HEAD", "tier1/merge-records")
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.endswith(".json")]


def _git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _committed_record(sb: Sandbox, record_id: str) -> tuple[dict[str, Any], bytes, str]:
    ref = f"tier1/merge-records/{record_id}.json"
    oid = _git_bytes(sb.root, "rev-parse", f"HEAD:{ref}").decode().strip()
    raw = _git_bytes(sb.root, "cat-file", "blob", oid)
    return json.loads(raw), raw, oid


def _var_snapshot(root: Path) -> tuple[tuple[Any, ...], ...]:
    """Capture directory entries as well as exact bytes, including empty dirs."""

    var = root / "var"
    if not var.exists() and not var.is_symlink():
        return ()
    paths = [var, *var.rglob("*")]
    rows: list[tuple[Any, ...]] = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            rows.append((relative, "symlink", mode, os.readlink(path)))
        elif path.is_dir():
            rows.append((relative, "dir", mode))
        elif path.is_file():
            rows.append((relative, "file", mode, path.read_bytes()))
        else:
            rows.append((relative, "other", mode))
    return tuple(rows)


def _physical_state(sb: Sandbox) -> dict[str, Any]:
    return {
        "head": _git_bytes(sb.root, "rev-parse", "HEAD"),
        "tree": _git_bytes(sb.root, "rev-parse", "HEAD^{tree}"),
        "head_file": (sb.root / ".git/HEAD").read_bytes(),
        "index": (sb.root / ".git/index").read_bytes(),
        "status": _git_bytes(
            sb.root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ),
        "var": _var_snapshot(sb.root),
    }


def _assert_json_ready(value: Any) -> None:
    assert not isinstance(value, Path)
    if isinstance(value, dict):
        for key, child in value.items():
            assert isinstance(key, str)
            _assert_json_ready(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_json_ready(child)
    json.dumps(value, ensure_ascii=False, allow_nan=False)


def _assert_common_plan(
    sb: Sandbox,
    plan: dict[str, Any],
    record_id: str,
    *,
    extra_keys: set[str] = frozenset(),
) -> dict[str, Any]:
    assert set(plan) == COMMON_KEYS | set(extra_keys)
    assert plan["record_id"] == record_id
    assert isinstance(plan["note"], str) and plan["note"]
    assert isinstance(plan["rules_fired"], list)
    assert set(plan["screen_ref"]) == SCREEN_REF_KEYS
    assert set(plan["fingerprints"]) == {
        "decision_head",
        "decision_tree",
        "merge_record",
        "screen_evidence",
    }

    committed, committed_bytes, git_oid = _committed_record(sb, record_id)
    fingerprints = plan["fingerprints"]
    assert fingerprints["decision_head"] == _git_bytes(
        sb.root, "rev-parse", "HEAD"
    ).decode().strip()
    assert fingerprints["decision_tree"] == _git_bytes(
        sb.root, "rev-parse", "HEAD^{tree}"
    ).decode().strip()
    assert fingerprints["merge_record"] == {
        "ref": f"tier1/merge-records/{record_id}.json",
        "git_oid": git_oid,
        "sha256": hashlib.sha256(committed_bytes).hexdigest(),
    }
    assert plan["screen_ref"] == {
        key: committed["screen"].get(key) for key in SCREEN_REF_KEYS
    }

    evidence = fingerprints["screen_evidence"]
    assert [row["kind"] for row in evidence] == ["output", "log"]
    for row in evidence:
        assert set(row) == {"kind", "ref", "sha256"}
        ref_key = f"{row['kind']}_ref"
        hash_key = f"{row['kind']}_sha256"
        if committed["screen"].get(ref_key) is not None:
            ref_key = f"{row['kind']}_ref"
            assert row == {
                "kind": row["kind"],
                "ref": committed["screen"][ref_key],
                "sha256": committed["screen"][hash_key],
            }
            assert hashlib.sha256((sb.root / row["ref"]).read_bytes()).hexdigest() == row[
                "sha256"
            ]
        else:
            assert row == {"kind": row["kind"], "ref": None, "sha256": None}
    _assert_json_ready(plan)
    return committed


def _screened_auto(sb: Sandbox) -> str:
    record_id = _create_pending(sb, "L1")
    transcribed = transcribe_engine_screen(sb, record_id)
    assert transcribed.returncode == 0, transcribed.stderr
    return record_id


def _screened_semantic(sb: Sandbox) -> str:
    _create_pending(sb, "L1")
    _create_pending(sb, "L2")
    record_id = _create_pending(sb, "L3")
    transcribed = transcribe_engine_screen(sb, record_id)
    assert transcribed.returncode == 0, transcribed.stderr
    return record_id


def test_prepare_decision_exact_auto_stage2_and_stuck_shapes(sandbox: Sandbox):
    auto_id = _screened_auto(sandbox)
    auto = prepare_decision(sandbox.root, auto_id)
    _assert_common_plan(sandbox, auto, auto_id)
    assert auto["route"] == "auto"
    assert auto["stage"] == 1
    assert auto["verdict"] == "land"
    assert auto["rules_fired"] == [{"rule_id": "R-ALLGREEN", "outcome": "land"}]

    semantic_id = _screened_semantic(sandbox)
    semantic = prepare_decision(sandbox.root, semantic_id)
    _assert_common_plan(sandbox, semantic, semantic_id, extra_keys={"packet_requirements"})
    assert semantic["route"] == "stage2"
    assert semantic["stage"] == 2
    assert semantic["verdict"] is None
    assert semantic["rules_fired"] == [
        {"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"}
    ]
    assert set(semantic["packet_requirements"]) == {
        "format",
        "source_snapshot",
        "input_hashes",
    }
    assert semantic["packet_requirements"]["format"] == "composition-gate-packet.v1"
    assert semantic["packet_requirements"]["source_snapshot"] == {
        "head_commit": semantic["fingerprints"]["decision_head"],
        "head_tree": semantic["fingerprints"]["decision_tree"],
    }
    assert semantic["packet_requirements"]["input_hashes"] is None
    assert not ({"attempt", "attempt_id", "model", "generator", "packet"} & set(semantic))

    stuck_id = _create_pending(sandbox, "L4")
    stuck = prepare_decision(sandbox.root, stuck_id)
    _assert_common_plan(sandbox, stuck, stuck_id, extra_keys={"rq"})
    assert stuck["route"] == "stuck"
    assert stuck["stage"] == 1
    assert stuck["verdict"] == "escalate-stuck"
    assert stuck["rules_fired"] == [
        {"rule_id": "R-SCREEN-INVALID", "outcome": "escalate-stuck"}
    ]
    assert stuck["rq"] == {
        "required": True,
        "kind": "cgate-escalation",
        "queued_by": "cgate",
        "payload_ref": None,
    }
    assert not ({"attempt", "attempt_id", "model", "generator", "packet"} & set(stuck))


def test_prepare_uses_committed_merge_record_bytes_not_dirty_worktree(sandbox: Sandbox):
    record_id = _screened_auto(sandbox)
    committed, committed_bytes, _oid = _committed_record(sandbox, record_id)
    record_path = sandbox.root / f"tier1/merge-records/{record_id}.json"
    dirty_bytes = b"{dirty working-tree bytes that are not JSON\n"
    record_path.write_bytes(dirty_bytes)
    before = _physical_state(sandbox)

    first = prepare_decision(sandbox.root, record_id)
    second = prepare_decision(sandbox.root, record_id)

    assert first == second
    assert first["route"] == "auto"
    assert first["screen_ref"] == {
        key: committed["screen"].get(key) for key in SCREEN_REF_KEYS
    }
    assert first["fingerprints"]["merge_record"]["sha256"] == hashlib.sha256(
        committed_bytes
    ).hexdigest()
    assert first["fingerprints"]["merge_record"]["sha256"] != hashlib.sha256(
        dirty_bytes
    ).hexdigest()
    assert _physical_state(sandbox) == before


def test_semantic_dry_run_never_allocates_attempt_or_invokes_generator(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _screened_semantic(sandbox)
    before = _physical_state(sandbox)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run decision crossed into stage-2 execution")

    monkeypatch.setattr(packet, "allocate_attempt", forbidden)
    monkeypatch.setattr(packet, "prepare_packet", forbidden)
    monkeypatch.setattr(stage2, "run_stage2", forbidden)

    plan = prepare_decision(sandbox.root, record_id)

    assert plan["route"] == "stage2"
    assert _physical_state(sandbox) == before
    assert not (sandbox.root / f"var/cgate/{record_id}/attempts").exists()


def test_invalid_dry_run_includes_rq_intent_and_writes_nothing(sandbox: Sandbox):
    record_id = _create_pending(sandbox, "L4")
    before = _physical_state(sandbox)

    plan = prepare_decision(sandbox.root, record_id)

    assert plan["route"] == "stuck"
    assert plan["rq"] == {
        "required": True,
        "kind": "cgate-escalation",
        "queued_by": "cgate",
        "payload_ref": None,
    }
    assert _physical_state(sandbox) == before


def test_completed_evidence_mismatch_routes_stuck_with_observed_fingerprint(
    sandbox: Sandbox,
):
    record_id = _screened_auto(sandbox)
    committed, _raw, _oid = _committed_record(sandbox, record_id)
    evidence_path = sandbox.root / committed["screen"]["output_ref"]
    evidence_path.write_bytes(b"tampered completed screen evidence\n")
    observed = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert observed != committed["screen"]["output_sha256"]
    before = _physical_state(sandbox)

    plan = prepare_decision(sandbox.root, record_id)

    assert plan["route"] == "stuck"
    assert plan["verdict"] == "escalate-stuck"
    assert plan["rq"]["required"] is True
    assert plan["fingerprints"]["screen_evidence"] == [
        {
            "kind": "output",
            "ref": committed["screen"]["output_ref"],
            "sha256": observed,
        },
        {
            "kind": "log",
            "ref": committed["screen"]["log_ref"],
            "sha256": observed,
        },
    ]
    assert _physical_state(sandbox) == before


def test_default_cli_is_canonical_deterministic_and_preserves_all_state(
    sandbox: Sandbox,
    capsys: pytest.CaptureFixture[str],
):
    record_id = _screened_auto(sandbox)
    before = _physical_state(sandbox)

    assert cgate_main(["decide", record_id, "--root", str(sandbox.root)]) == 0
    first = capsys.readouterr()
    assert first.err == ""
    assert cgate_main(["decide", record_id, "--root", str(sandbox.root)]) == 0
    second = capsys.readouterr()

    assert second.err == ""
    assert first.out == second.out
    assert first.out == render_decision(prepare_decision(sandbox.root, record_id))
    assert first.out.endswith("\n")
    assert json.loads(first.out)["route"] == "auto"
    assert _physical_state(sandbox) == before


def test_decide_rejects_out_and_execute_auto_commits_exact_stage1_linkage(
    sandbox: Sandbox,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _screened_auto(sandbox)
    before = _physical_state(sandbox)

    with pytest.raises(SystemExit) as out_rejected:
        cgate_main(
            [
                "decide",
                record_id,
                "--root",
                str(sandbox.root),
                "--out",
                "var/forbidden.json",
            ]
        )
    captured = capsys.readouterr()
    assert out_rejected.value.code == 2
    assert "unrecognized arguments: --out var/forbidden.json" in captured.err
    assert _physical_state(sandbox) == before

    monkeypatch.setenv("HT_ROLE", "cgate")
    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    assert cgate_main(
        ["decide", record_id, "--root", str(sandbox.root), "--execute"]
    ) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["verdict"] == "land"
    assert result["ratification_ref"] is None
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != head_before
    review = sandbox.load("tier1/gate-reviews/GR-1.json")
    assert review["stage"] == 1
    assert review["attempt_id"] is None
    assert review["packet"] is None
    assert review["template"] is None
    assert review["generator"] is None
    assert review["rules_fired"] == []
    assert review["observations"] == []
    assert review["raw_output"] is None
    assert review["escalation_ref"] is None
    review_hash = hashlib.sha256(jsonio.dumps(review).encode()).hexdigest()
    assert result["gate_review_sha256"] == review_hash
    assert sandbox.load(f"tier1/merge-records/{record_id}.json")["gate_verdict"] == {
        "verdict": "land",
        "date": review["created"],
        "review_ref": "GR-1",
        "review_sha256": review_hash,
        "note": review["note"],
    }
    assert not list((sandbox.root / "tier1/ratification-queue").glob("RQ-*.json"))


def test_execute_invalid_birth_creates_gr_rq_mr_without_generator(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _create_pending(sandbox, "L4")
    monkeypatch.setenv("HT_ROLE", "cgate")

    class ForbiddenGenerator:
        def generate(self, _request):
            raise AssertionError("invalid route invoked a generator")

    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    result = execute_decision(
        sandbox.root, record_id, generator=ForbiddenGenerator()
    )
    assert sandbox.git("rev-list", "--count", f"{head_before}..HEAD").stdout.strip() == "1"
    assert result["verdict"] == "escalate-stuck"
    assert result["ratification_ref"] == "RQ-1"
    review = sandbox.load("tier1/gate-reviews/GR-1.json")
    assert review["stage"] == 1
    assert review["rules_fired"] == []
    assert review["screen_ref"] == {key: None for key in SCREEN_REF_KEYS}
    assert review["escalation_ref"] == "RQ-1"
    rq = sandbox.load("tier1/ratification-queue/RQ-1.json")
    assert rq == {
        "id": "RQ-1",
        "kind": "cgate-escalation",
        "payload_ref": "tier1/gate-reviews/GR-1.json",
        "text": review["note"],
        "queued_by": "cgate",
        "date": review["created"],
        "disposition": None,
        "annotations": [],
    }


def _screened_semantic_with_packet(sb: Sandbox) -> str:
    initialized = sb.run(
        "tree", "init", "L4",
        "--root-question", "Synthetic composition question",
        role="director",
    )
    assert initialized.returncode == 0, initialized.stderr
    dispatch_id = seed_worked_node(sb)
    completed = sb.run(
        "dispatch", "outcome",
        "--dispatch", dispatch_id,
        "--outcome", "completed",
        role="harness",
    )
    assert completed.returncode == 0, completed.stderr
    for _ in range(3):
        adjudication_ref = seed_mrec_candidate(sb, "tree#L4/node#1")
        created = sb.run(
            "mrec", "create",
            "--candidate-ref", "tree#L4/node#1",
            "--lane-verdict", "lane-pass",
            "--lane-adjudication-ref", adjudication_ref,
            "--scope-lane", "L4",
            role="harness",
        )
        assert created.returncode == 0, created.stderr
    record_id = "MR-3"
    transcribed = transcribe_engine_screen(sb, record_id)
    assert transcribed.returncode == 0, transcribed.stderr
    return record_id


def test_execute_stage2_uses_injected_generator_then_one_atomic_finalization(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _screened_semantic_with_packet(sandbox)
    monkeypatch.setenv("HT_ROLE", "cgate")

    class SyntheticGenerator:
        calls = 0

        def generate(self, _request):
            self.calls += 1
            return GenerationResult.synthetic(
                {
                    "verdict": "hold",
                    "note": "Hold until scope-overlap and queue-adjacency are resolved.",
                    "observations": [],
                },
                session_id="synthetic-d2",
            )

    generator = SyntheticGenerator()
    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    result = execute_decision(
        sandbox.root,
        record_id,
        generator=generator,
        attempt_id="1" * 32,
    )
    assert generator.calls == 1
    assert sandbox.git("rev-list", "--count", f"{head_before}..HEAD").stdout.strip() == "1"
    assert result["verdict"] == "hold"
    assert result["ratification_ref"] is None
    review = sandbox.load("tier1/gate-reviews/GR-1.json")
    assert review["stage"] == 2
    assert review["attempt_id"] == "1" * 32
    assert review["generator"] == {
        "mechanism": "injected-synthetic",
        "status": "synthetic",
        "requested_model": None,
        "actual_model": "<synthetic>",
        "session_ref": "synthetic-d2",
        "error": None,
    }
    for ref, digest in review["packet"]["input_hashes"].items():
        artifact = sandbox.root / f"var/cgate/{record_id}/attempts/{'1' * 32}" / ref
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
    assert hashlib.sha256((sandbox.root / review["raw_output"]["ref"]).read_bytes()).hexdigest() == review["raw_output"]["sha256"]


def test_stage2_packet_failure_records_stuck_without_generator(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _screened_semantic(sandbox)
    monkeypatch.setenv("HT_ROLE", "cgate")

    class ForbiddenGenerator:
        def generate(self, _request):
            raise AssertionError("packet-invalid route invoked a generator")

    result = execute_decision(
        sandbox.root,
        record_id,
        generator=ForbiddenGenerator(),
        attempt_id="2" * 32,
    )
    assert result["verdict"] == "escalate-stuck"
    assert result["ratification_ref"] == "RQ-1"
    review = sandbox.load("tier1/gate-reviews/GR-1.json")
    assert review["stage"] == 2
    assert review["generator"]["status"] == "technical-failure"
    assert review["generator"]["error"]["kind"] == "report-join-missing"
    context = sandbox.load(
        f"var/cgate/{record_id}/attempts/{'2' * 32}/packet/context.json"
    )
    assert context["packet_status"] == "technical-failure"
    assert context["error"]["kind"] == "report-join-missing"


def test_stage2_multibyte_technical_failure_finalizes_stuck_with_exact_evidence(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _screened_semantic_with_packet(sandbox)
    monkeypatch.setenv("HT_ROLE", "cgate")

    class TechnicalFailureGenerator:
        def generate(self, request):
            detail = str(request.research_root.resolve()) + "\n\t" + ("é " * 800)
            return GenerationResult(
                mechanism="claude-p",
                return_code=None,
                technical_error="sandbox-probe-failure",
                technical_detail=detail,
            )

    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    result = execute_decision(
        sandbox.root,
        record_id,
        generator=TechnicalFailureGenerator(),
        attempt_id="9" * 32,
    )

    assert sandbox.git("rev-list", "--count", f"{head_before}..HEAD").stdout.strip() == "1"
    assert result["verdict"] == "escalate-stuck"
    assert result["ratification_ref"] == "RQ-1"
    review = sandbox.load("tier1/gate-reviews/GR-1.json")
    raw = sandbox.load(review["raw_output"]["ref"])
    message = review["generator"]["error"]["message"]
    assert review["stage"] == 2
    assert review["generator"]["error"]["kind"] == "sandbox-probe-failure"
    assert len(message.encode("utf-8")) <= 1024
    assert str(sandbox.root.resolve()) not in message
    assert raw["generation"]["technical_detail"] == message
    assert raw["technical_detail"] == message
    rq = sandbox.load("tier1/ratification-queue/RQ-1.json")
    assert rq["payload_ref"] == "tier1/gate-reviews/GR-1.json"
    assert rq["text"] == review["note"]


def test_stale_finalizer_rejects_without_tier1_mutation(sandbox: Sandbox):
    record_id = _screened_auto(sandbox)
    decision = prepare_decision(sandbox.root, record_id)
    payload = {
        "format": FINALIZATION_FORMAT,
        "decision": decision,
        "stage2": None,
    }
    moved = sandbox.run(
        "tree", "init", "stale-proof",
        "--root-question", "Synthetic stale fingerprint proof",
        role="director",
    )
    assert moved.returncode == 0, moved.stderr
    moved_head = sandbox.git("rev-parse", "HEAD").stdout.strip()
    before = sorted(
        path.relative_to(sandbox.root).as_posix()
        for path in (sandbox.root / "tier1").rglob("*.json")
    )
    with pytest.raises(DecisionError, match="stale physical HEAD/tree"):
        finalize_prepared(sandbox.root, payload)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == moved_head
    after = sorted(
        path.relative_to(sandbox.root).as_posix()
        for path in (sandbox.root / "tier1").rglob("*.json")
    )
    assert after == before


def test_changed_or_symlinked_screen_evidence_fails_closed(sandbox: Sandbox):
    record_id = _screened_auto(sandbox)
    decision = prepare_decision(sandbox.root, record_id)
    payload = {
        "format": FINALIZATION_FORMAT,
        "decision": decision,
        "stage2": None,
    }
    evidence_ref = decision["screen_ref"]["output_ref"]
    evidence = sandbox.root / evidence_ref
    original = evidence.read_bytes()
    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()

    evidence.write_bytes(original + b"changed\n")
    with pytest.raises(DecisionError, match="changed screen evidence bytes"):
        finalize_prepared(sandbox.root, payload)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert not list((sandbox.root / "tier1/gate-reviews").glob("GR-*.json"))

    backup = evidence.with_name("identical-screen-backup.json")
    backup.write_bytes(original)
    evidence.unlink()
    evidence.symlink_to(backup.name)
    with pytest.raises(DecisionError, match="symlink/path swap"):
        finalize_prepared(sandbox.root, payload)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert not list((sandbox.root / "tier1/gate-reviews").glob("GR-*.json"))


def test_stage2_manifest_raw_and_decision_mutations_fail_before_tier1_write(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _screened_semantic_with_packet(sandbox)
    monkeypatch.setenv("HT_ROLE", "cgate")

    class SyntheticGenerator:
        def generate(self, _request):
            return GenerationResult.synthetic(
                {
                    "verdict": "hold",
                    "note": "Hold until scope-overlap and queue-adjacency are resolved.",
                    "observations": [],
                },
                session_id="synthetic-mutation-proof",
            )

    captured: dict[str, Any] = {}
    real_finalize = root_cgate.finalize_prepared

    def capture(_root, payload):
        captured.update(payload)
        return {
            "record_id": record_id,
            "gate_review_ref": None,
            "gate_review_sha256": None,
            "verdict": payload["stage2"]["verdict"],
            "ratification_ref": None,
        }

    monkeypatch.setattr(root_cgate, "finalize_prepared", capture)
    execute_decision(
        sandbox.root,
        record_id,
        generator=SyntheticGenerator(),
        attempt_id="3" * 32,
    )
    monkeypatch.setattr(root_cgate, "finalize_prepared", real_finalize)
    assert captured["format"] == FINALIZATION_FORMAT
    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    refs = [
        captured["stage2"]["packet"]["manifest_ref"],
        captured["stage2"]["raw_output"]["ref"],
        captured["stage2"]["decision_output"]["ref"],
    ]
    for ref in refs:
        path = sandbox.root / ref
        original = path.read_bytes()
        path.write_bytes(original + b"tampered\n")
        with pytest.raises(DecisionError):
            real_finalize(sandbox.root, captured)
        path.write_bytes(original)
        assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
        assert not list((sandbox.root / "tier1/gate-reviews").glob("GR-*.json"))


def test_finalizer_rejects_prepared_decision_that_diverges_from_raw_stage2_output(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
):
    record_id = _screened_semantic_with_packet(sandbox)
    monkeypatch.setenv("HT_ROLE", "cgate")

    class SyntheticGenerator:
        def generate(self, _request):
            return GenerationResult.synthetic(
                {
                    "verdict": "hold",
                    "note": "Hold until scope-overlap and queue-adjacency are resolved.",
                    "observations": [],
                },
                session_id="synthetic-forged-decision-repro",
            )

    captured: dict[str, Any] = {}
    real_finalize = root_cgate.finalize_prepared

    def capture(_root, payload):
        captured.update(payload)
        return {
            "record_id": record_id,
            "gate_review_ref": None,
            "gate_review_sha256": None,
            "verdict": payload["stage2"]["verdict"],
            "ratification_ref": None,
        }

    monkeypatch.setattr(root_cgate, "finalize_prepared", capture)
    execute_decision(
        sandbox.root,
        record_id,
        generator=SyntheticGenerator(),
        attempt_id="4" * 32,
    )
    monkeypatch.setattr(root_cgate, "finalize_prepared", real_finalize)

    stage2_payload = captured["stage2"]
    decision_path = sandbox.root / stage2_payload["decision_output"]["ref"]
    original_decision_bytes = decision_path.read_bytes()
    original_decision_hash = stage2_payload["decision_output"]["sha256"]
    original_generator = json.loads(json.dumps(stage2_payload["generator"]))
    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()

    stage2_payload["generator"]["session_ref"] = "forged-session"
    decision_document = json.loads(original_decision_bytes)
    decision_document["generator"] = stage2_payload["generator"]
    decision_bytes = jsonio.dumps(decision_document).encode("utf-8")
    decision_path.write_bytes(decision_bytes)
    stage2_payload["decision_output"]["sha256"] = hashlib.sha256(
        decision_bytes
    ).hexdigest()
    with pytest.raises(DecisionError, match="stage-2"):
        real_finalize(sandbox.root, captured)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert not list((sandbox.root / "tier1/gate-reviews").glob("GR-*.json"))

    stage2_payload["generator"] = original_generator
    stage2_payload["decision_output"]["sha256"] = original_decision_hash
    decision_path.write_bytes(original_decision_bytes)
    stage2_payload["verdict"] = "land-after-MR-999"
    stage2_payload["note"] = "Forged sequencing after MR-999."
    decision_document = json.loads(decision_path.read_text(encoding="utf-8"))
    decision_document["verdict"] = stage2_payload["verdict"]
    decision_document["note"] = stage2_payload["note"]
    decision_bytes = jsonio.dumps(decision_document).encode("utf-8")
    decision_path.write_bytes(decision_bytes)
    stage2_payload["decision_output"]["sha256"] = hashlib.sha256(
        decision_bytes
    ).hexdigest()

    with pytest.raises(DecisionError, match="stage-2"):
        real_finalize(sandbox.root, captured)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert not list((sandbox.root / "tier1/gate-reviews").glob("GR-*.json"))

    stage2_payload["verdict"] = "hold"
    stage2_payload["note"] = (
        "Hold until scope-overlap and queue-adjacency are resolved."
    )
    stage2_payload["decision_output"]["sha256"] = original_decision_hash
    decision_path.write_bytes(original_decision_bytes)
    raw_path = sandbox.root / stage2_payload["raw_output"]["ref"]
    raw_document = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_document["stdout"] = jsonio.dumps(
        {
            "verdict": "land-after-MR-999",
            "note": "Forged sequencing after MR-999.",
            "observations": [],
        }
    )
    raw_bytes = jsonio.dumps(raw_document).encode("utf-8")
    raw_path.write_bytes(raw_bytes)
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    stage2_payload["raw_output"]["sha256"] = raw_hash
    stage2_payload["verdict"] = "land-after-MR-999"
    stage2_payload["note"] = "Forged sequencing after MR-999."
    decision_document = json.loads(original_decision_bytes)
    decision_document["raw_output"]["sha256"] = raw_hash
    decision_document["verdict"] = stage2_payload["verdict"]
    decision_document["note"] = stage2_payload["note"]
    decision_bytes = jsonio.dumps(decision_document).encode("utf-8")
    decision_path.write_bytes(decision_bytes)
    stage2_payload["decision_output"]["sha256"] = hashlib.sha256(
        decision_bytes
    ).hexdigest()
    with pytest.raises(DecisionError, match="stage-2"):
        real_finalize(sandbox.root, captured)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert not list((sandbox.root / "tier1/gate-reviews").glob("GR-*.json"))


@pytest.mark.parametrize("record_id", ["MR-01", "mr-1", "MR-1/../../outside", " MR-1"])
def test_malformed_record_ids_fail_closed_without_writes(
    sandbox: Sandbox,
    record_id: str,
):
    before = _physical_state(sandbox)
    with pytest.raises(DecisionError):
        prepare_decision(sandbox.root, record_id)
    assert _physical_state(sandbox) == before


def test_missing_uncommitted_and_malformed_committed_records_fail_without_writes(
    sandbox: Sandbox,
):
    before = _physical_state(sandbox)
    with pytest.raises(DecisionError):
        prepare_decision(sandbox.root, "MR-999")
    assert _physical_state(sandbox) == before

    sandbox.write_file("tier1/merge-records/MR-2.json", "{}\n")
    before = _physical_state(sandbox)
    with pytest.raises(DecisionError):
        prepare_decision(sandbox.root, "MR-2")
    assert _physical_state(sandbox) == before

    malformed = sandbox.root / "tier1/merge-records/MR-9.json"
    malformed.write_text("{malformed committed JSON\n", encoding="utf-8")
    assert sandbox.git("add", "--", "tier1/merge-records/MR-9.json").returncode == 0
    committed = sandbox.git("commit", "--no-verify", "-m", "malformed synthetic MR")
    assert committed.returncode == 0, committed.stderr
    before = _physical_state(sandbox)
    with pytest.raises(DecisionError):
        prepare_decision(sandbox.root, "MR-9")
    assert _physical_state(sandbox) == before


def _isolated_cli(
    tmp_path: Path,
    source_paths: list[Path],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in source_paths)
    env["HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, "-S", "-m", "composition_gate.cli", *arguments],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _build_standalone_wheel(tmp_path: Path) -> Path:
    wheel_dir = tmp_path / "standalone-wheel"
    wheel_dir.mkdir()
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    built = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
            str(COMPOSITION_SOURCE),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheels = list(wheel_dir.glob("composition_gate-*.whl"))
    assert len(wheels) == 1
    installed = tmp_path / "standalone-installed"
    with zipfile.ZipFile(wheels[0]) as archive:
        archive.extractall(installed)
    return installed


def test_standalone_wheel_keeps_screen_and_rejects_decide_while_root_source_provides_it(
    sandbox: Sandbox,
    tmp_path: Path,
):
    record_id = _screened_auto(sandbox)
    standalone_wheel = _build_standalone_wheel(tmp_path)

    standalone_screen = _isolated_cli(
        tmp_path,
        [standalone_wheel],
        "screen",
        record_id,
        "--root",
        str(sandbox.root),
    )
    root_screen = _isolated_cli(
        tmp_path,
        [COMPOSITION_SOURCE, ROOT_SYSTEM_SOURCE],
        "screen",
        record_id,
        "--root",
        str(sandbox.root),
    )
    assert standalone_screen.returncode == root_screen.returncode == 0
    assert standalone_screen.stderr == root_screen.stderr == ""
    assert standalone_screen.stdout == root_screen.stdout
    assert set(json.loads(standalone_screen.stdout)) == {
        "record_id",
        "computed",
        "config_hash",
        "engine_version",
        "head_commit",
        "head_tree",
        "results",
    }

    standalone_decide = _isolated_cli(
        tmp_path,
        [standalone_wheel],
        "decide",
        record_id,
        "--root",
        str(sandbox.root),
    )
    assert standalone_decide.returncode == 2
    assert standalone_decide.stdout == ""
    assert STANDALONE_DECIDE_ERROR in standalone_decide.stderr

    root_decide = _isolated_cli(
        tmp_path,
        [COMPOSITION_SOURCE, ROOT_SYSTEM_SOURCE],
        "decide",
        record_id,
        "--root",
        str(sandbox.root),
    )
    assert root_decide.returncode == 0, root_decide.stderr
    assert root_decide.stderr == ""
    assert json.loads(root_decide.stdout)["route"] == "auto"
