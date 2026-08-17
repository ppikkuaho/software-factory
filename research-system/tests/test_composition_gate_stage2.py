"""Phase C2: detached, sandboxed, evidence-complete stage-2 preparation."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from composition_gate import stage2 as stage2_module
from composition_gate.packet import MANIFEST_FORMAT, PreparedPacket
from composition_gate.stage2 import (
    ALLOWED_TOOLS,
    ClaudeGenerator,
    GenerationRequest,
    GenerationResult,
    MacOSSandbox,
    RAW_OUTPUT_FORMAT,
    REQUESTED_MODEL,
    SandboxError,
    SandboxLaunch,
    Stage2Error,
    ValidatedDecision,
    child_environment,
    cleanup_detached,
    detach_packet,
    prepare_technical_failure,
    run_stage2,
    verify_stage2_raw_evidence,
    write_raw_output,
    write_decision_output,
)


ATTEMPT = "1234567890abcdef1234567890abcdef"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _canonical(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _prepared(tmp_path: Path, name: str = "case", *, with_pcd: bool = True) -> tuple[Path, PreparedPacket]:
    root = tmp_path / name / "research"
    attempt_dir = root / f"var/cgate/MR-1/attempts/{ATTEMPT}"
    packet_dir = attempt_dir / "packet"
    packet_dir.mkdir(parents=True)
    context = {
        "format": "composition-gate-packet.v1",
        "operative_merge_schedule_pc_decision": "PCD-9" if with_pcd else None,
    }
    payloads = {
        "packet/context.json": _canonical(context),
        "packet/001-report.md": b"# Synthetic candidate report\n",
        "packet/002-candidate-node.json": _canonical({"id": "1", "synthetic": True}),
    }
    source_rows = {
        "packet/context.json": ("generated:packet-context", None, "packet-context"),
        "packet/001-report.md": (
            "trees/L4/nodes/1/reports/synthetic.md",
            "1" * 40,
            "candidate-report",
        ),
        "packet/002-candidate-node.json": (
            "trees/L4/nodes/1/node.json",
            "2" * 40,
            "candidate-node",
        ),
    }
    if with_pcd:
        payloads["packet/003-pc-decision-PCD-9.json"] = _canonical(
            {"id": "PCD-9", "kind": "merge-schedule", "context_refs": ["MR-1"]}
        )
        source_rows["packet/003-pc-decision-PCD-9.json"] = (
            "tier1/decision-log/PCD-9.json",
            "3" * 40,
            "pc-decision",
        )
    for ref, content in payloads.items():
        _write(attempt_dir / ref, content)
    artifacts = [
        {
            "source_ref": source_rows[ref][0],
            "git_oid": source_rows[ref][1],
            "packet_ref": ref,
            "artifact_kind": source_rows[ref][2],
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for ref, content in payloads.items()
    ]
    hashes = {row["packet_ref"]: row["sha256"] for row in artifacts}
    manifest = {
        "format": MANIFEST_FORMAT,
        "attempt_id": ATTEMPT,
        "record_id": "MR-1",
        "snapshot": {"head_commit": "a" * 40, "head_tree": "b" * 40},
        "artifacts": artifacts,
        "artifact_refs": list(hashes),
        "input_hashes": hashes,
        "source_refs": {row["packet_ref"]: row["source_ref"] for row in artifacts},
    }
    manifest_bytes = _canonical(manifest)
    _write(packet_dir / "manifest.json", manifest_bytes)
    prepared = PreparedPacket(
        attempt_id=ATTEMPT,
        attempt_dir=attempt_dir,
        packet_dir=packet_dir,
        manifest_ref=f"var/cgate/MR-1/attempts/{ATTEMPT}/packet/manifest.json",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artifact_refs=tuple(hashes),
        input_hashes=hashes,
    )
    return root, prepared


def _row(check: str, *, inputs: dict | None = None, failed: bool = False) -> dict:
    return {
        "check": check,
        "result": "fail" if failed else "pass",
        "detail": "synthetic condition evidence",
        "inputs": inputs or {},
    }


def _record(failures: dict[str, dict]) -> dict:
    return {
        "id": "MR-1",
        "screen": {
            "results": [
                _row(check, inputs=failures.get(check), failed=check in failures)
                for check in (
                    "scope-overlap",
                    "surface-budget",
                    "settlement-completeness",
                    "queue-adjacency",
                    "watch-debt",
                )
            ]
        },
    }


SURFACE = _record(
    {
        "surface-budget": {
            "candidate": {"scope": {"surfaces": ["actor/surface"]}},
            "surface_counts": {"actor/surface": {"count": 12, "records": []}},
        }
    }
)
WATCH = _record(
    {
        "watch-debt": {
            "overlapping_watch_rows": [{"row_id": "W-7"}],
        }
    }
)
SCOPE = _record(
    {
        "scope-overlap": {
            "collisions": [{"record_id": "MR-2"}],
        }
    }
)
QUEUE = _record(
    {
        "queue-adjacency": {
            "pending_records": [{"record_id": "MR-1"}, {"record_id": "MR-2"}],
        }
    }
)
SETTLEMENT = _record(
    {
        "settlement-completeness": {
            "overdue_rows": [{"tree_path": "trees/L4/tree.json", "row_id": "W-8"}],
        }
    }
)
MULTI = _record(
    {
        "surface-budget": SURFACE["screen"]["results"][1]["inputs"],
        "watch-debt": WATCH["screen"]["results"][4]["inputs"],
    }
)
SCOPE_AND_SURFACE = _record(
    {
        "scope-overlap": SCOPE["screen"]["results"][0]["inputs"],
        "surface-budget": SURFACE["screen"]["results"][1]["inputs"],
    }
)


@dataclass
class StaticGenerator:
    result: GenerationResult
    calls: int = 0

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        assert request.detached.packet_dir.is_dir()
        assert not request.detached.root.is_relative_to(request.research_root)
        return self.result


@dataclass
class BoundaryPathGenerator:
    path_kind: str
    stream: str
    protected_path: str | None = None

    def generate(self, request: GenerationRequest) -> GenerationResult:
        paths = {
            "research-root": request.research_root,
            "detached-packet": request.detached.root,
            "temporary-home": request.detached.home_dir,
        }
        protected = str(paths[self.path_kind])
        self.protected_path = protected
        limit = 32 * 1024 if self.stream == "stdout" else 16 * 1024
        content = b"x" * (limit - len(protected.encode()) // 2) + protected.encode() + b"tail"
        return GenerationResult(
            mechanism="claude-p",
            stdout=content if self.stream == "stdout" else b"",
            stderr=content if self.stream == "stderr" else b"",
            return_code=9,
        )


@dataclass
class InvalidResultGenerator:
    calls: int = 0

    def generate(self, request: GenerationRequest) -> object:
        self.calls += 1
        return object()


def _body(verdict: str, note: str = "Synthetic compositional decision.", observations=None) -> dict:
    return {
        "verdict": verdict,
        "note": note,
        "observations": [] if observations is None else observations,
    }


def _run(
    tmp_path: Path,
    name: str,
    body: dict | str | bytes,
    *,
    record: dict = SURFACE,
    rules=(),
    session: str = "synthetic-session-1",
):
    root, prepared = _prepared(tmp_path, name)
    generator = StaticGenerator(GenerationResult.synthetic(body, session_id=session))
    result = run_stage2(prepared, root, record, rules, generator)
    return root, prepared, generator, result


def _raw(prepared: PreparedPacket, result) -> dict:
    path = prepared.attempt_dir / "raw-output.json"
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == result.raw_output.sha256
    assert result.raw_output.ref == f"var/cgate/MR-1/attempts/{ATTEMPT}/raw-output.json"
    envelope = json.loads(content)
    assert envelope["format"] == RAW_OUTPUT_FORMAT
    assert set(envelope) == {
        "format",
        "generation",
        "generator",
        "stdout",
        "stdout_truncated",
        "stderr",
        "stderr_truncated",
        "return_code",
        "technical_error",
        "technical_detail",
    }
    decision_path = prepared.attempt_dir / "decision.json"
    decision_bytes = decision_path.read_bytes()
    assert hashlib.sha256(decision_bytes).hexdigest() == result.decision_output.sha256
    assert result.decision_output.ref == (
        f"var/cgate/MR-1/attempts/{ATTEMPT}/decision.json"
    )
    decision = json.loads(decision_bytes)
    assert decision["raw_output"] == {
        "ref": result.raw_output.ref,
        "sha256": result.raw_output.sha256,
    }
    return envelope


def _reconstruct(prepared: PreparedPacket, result, record: dict, rules=()) -> dict:
    raw = json.loads((prepared.attempt_dir / "raw-output.json").read_text())
    manifest = json.loads((prepared.packet_dir / "manifest.json").read_text())
    return verify_stage2_raw_evidence(
        raw,
        prepared=prepared,
        record=record,
        rules_fired=rules,
        manifest=manifest,
    )


def test_stage2_accepts_zero_observations_and_records_exact_synthetic_provenance(tmp_path: Path):
    root, prepared, generator, result = _run(
        tmp_path,
        "zero",
        _body("bounce-for-surface-rework"),
    )
    assert result.verdict == "bounce-for-surface-rework"
    assert result.observations == ()
    assert result.error_kind is None
    assert result.generator == {
        "mechanism": "injected-synthetic",
        "status": "synthetic",
        "requested_model": None,
        "actual_model": "<synthetic>",
        "session_ref": "synthetic-session-1",
        "error": None,
    }
    assert generator.calls == 1
    assert _raw(prepared, result)["technical_error"] is None
    assert root not in [prepared.attempt_dir]
    assert _reconstruct(prepared, result, SURFACE) == {
        "generator": result.generator,
        "verdict": result.verdict,
        "note": result.note,
        "observations": list(result.observations),
        "error_kind": None,
    }


def test_raw_reconstruction_rejects_generator_and_binding_tampering(tmp_path: Path):
    _, prepared, _, result = _run(
        tmp_path,
        "reconstruction-tamper",
        _body("consolidate-first", "Consolidate pending MR-1 and MR-2 first."),
        record=QUEUE,
        rules=({"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"},),
    )
    raw = _raw(prepared, result)
    manifest = json.loads((prepared.packet_dir / "manifest.json").read_text())

    forged_generator = json.loads(json.dumps(raw))
    forged_generator["generator"]["session_ref"] = "forged-session"
    with pytest.raises(Stage2Error, match="provenance"):
        verify_stage2_raw_evidence(
            forged_generator,
            prepared=prepared,
            record=QUEUE,
            rules_fired=(
                {"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"},
            ),
            manifest=manifest,
        )

    forged_body = json.loads(json.dumps(raw))
    forged_body["stdout"] = _canonical(
        _body("hold", "Hold until pending MR-1 and MR-2 resolve.")
    ).decode()
    with pytest.raises(Stage2Error, match="reconstruct"):
        verify_stage2_raw_evidence(
            forged_body,
            prepared=prepared,
            record=QUEUE,
            rules_fired=(
                {"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"},
            ),
            manifest=manifest,
        )


def test_stage2_converts_each_observation_ref_to_manifest_hash_anchor(tmp_path: Path):
    _, prepared, _, result = _run(
        tmp_path,
        "anchored",
        _body(
            "bounce-for-surface-rework",
            observations=[
                {
                    "text": "Synthetic coordination observation.",
                    "artifact_refs": ["packet/001-report.md", "packet/context.json"],
                }
            ],
        ),
    )
    assert result.observations == (
        {
            "text": "Synthetic coordination observation.",
            "anchors": [
                {
                    "ref": "packet/001-report.md",
                    "sha256": prepared.input_hashes["packet/001-report.md"],
                },
                {
                    "ref": "packet/context.json",
                    "sha256": prepared.input_hashes["packet/context.json"],
                },
            ],
        },
    )


@pytest.mark.parametrize(
    ("name", "verdict", "note", "record", "rules", "observations"),
    [
        (
            "land-after",
            "land-after-MR-2",
            "Sequence after MR-2.",
            SCOPE,
            ({"rule_id": "R-OVERLAP-SEQ", "outcome": "land-after-MR-2"},),
            [],
        ),
        (
            "consolidate",
            "consolidate-first",
            "Consolidate pending MR-1 and MR-2 first.",
            QUEUE,
            ({"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"},),
            [],
        ),
        (
            "settlement-hold",
            "hold",
            "Hold until W-8 is resolved.",
            SETTLEMENT,
            ({"rule_id": "R-SETTLE-HOLD", "outcome": "hold"},),
            [],
        ),
        ("bounce", "bounce-for-surface-rework", "Rework the surface.", SURFACE, (), []),
        (
            "surface-hold",
            "hold",
            "Hold until actor/surface clears its mechanical budget.",
            SURFACE,
            (),
            [],
        ),
        ("hold", "hold", "Hold until W-7 resolves.", WATCH, (), []),
        (
            "scope-bounce",
            "bounce-for-surface-rework",
            "Rework the overlapping scope.",
            SCOPE,
            (),
            [],
        ),
        (
            "scope-hold",
            "hold",
            "Hold until sequencing after MR-2 resolves the scope overlap.",
            SCOPE,
            (),
            [],
        ),
        (
            "queue-hold",
            "hold",
            "Hold until pending MR-1 and MR-2 are resolved.",
            QUEUE,
            (),
            [],
        ),
        (
            "multi-hold",
            "hold",
            "Hold until the combined mechanical failures resolve.",
            MULTI,
            (),
            [],
        ),
        (
            "multi-bounce",
            "bounce-for-surface-rework",
            "Surface rework also names watch-debt as the other unblock.",
            MULTI,
            (),
            [],
        ),
        (
            "scope-surface-bounce",
            "bounce-for-surface-rework",
            "Rework the combined scope and surface failure.",
            SCOPE_AND_SURFACE,
            (),
            [],
        ),
        (
            "user",
            "escalate-to-user",
            "Actor-visible guidance promotion needs user judgment.",
            SURFACE,
            (),
            [{"text": "Synthetic promotion evidence.", "artifact_refs": ["packet/001-report.md"]}],
        ),
        (
            "stuck",
            "escalate-stuck",
            "Missing contradictory packet evidence prevents a decision.",
            SURFACE,
            (),
            [],
        ),
    ],
)
def test_each_condition_eligible_non_land_token(
    tmp_path: Path,
    name: str,
    verdict: str,
    note: str,
    record: dict,
    rules,
    observations,
):
    _, prepared, _, result = _run(
        tmp_path,
        name,
        _body(verdict, note, observations),
        record=record,
        rules=rules,
    )
    assert result.verdict == verdict
    assert result.error_kind is None
    assert _raw(prepared, result)["technical_error"] is None


def test_gate_versus_pc_escalation_requires_the_operative_pcd_anchor(tmp_path: Path):
    note = "The gate and PC disagree in a concrete merge-schedule conflict."
    _, _, _, accepted = _run(
        tmp_path,
        "pc-accepted",
        _body(
            "escalate-to-user",
            note,
            [{"text": "Synthetic conflict.", "artifact_refs": ["packet/003-pc-decision-PCD-9.json"]}],
        ),
    )
    assert accepted.verdict == "escalate-to-user"
    _, _, _, rejected = _run(
        tmp_path,
        "pc-rejected",
        _body(
            "escalate-to-user",
            note,
            [{"text": "Synthetic conflict.", "artifact_refs": ["packet/001-report.md"]}],
        ),
    )
    assert rejected.verdict == "escalate-stuck"
    assert rejected.error_kind == "reserved-escalation-ineligible"


def test_directive_budget_reserved_escalation_requires_report_anchor_and_no_consolidation_path(
    tmp_path: Path,
):
    note = "Directive budget growth on core surfaces has no consolidation path."
    _, _, _, accepted = _run(
        tmp_path,
        "directive-accepted",
        _body(
            "escalate-to-user",
            note,
            [{"text": "Synthetic budget evidence.", "artifact_refs": ["packet/001-report.md"]}],
        ),
    )
    assert accepted.error_kind is None
    _, _, _, rejected = _run(
        tmp_path,
        "directive-rejected",
        _body(
            "escalate-to-user",
            note,
            [{"text": "Synthetic budget evidence.", "artifact_refs": ["packet/context.json"]}],
        ),
    )
    assert rejected.error_kind == "reserved-escalation-ineligible"


@pytest.mark.parametrize(
    ("name", "verdict", "record", "note"),
    [
        ("surface-consolidate", "consolidate-first", SURFACE, "Consolidate first."),
        ("watch-bounce", "bounce-for-surface-rework", WATCH, "Rework the surface."),
        ("scope-consolidate", "consolidate-first", SCOPE, "Consolidate first."),
        ("queue-bounce", "bounce-for-surface-rework", QUEUE, "Rework the surface."),
        (
            "multi-consolidate",
            "consolidate-first",
            MULTI,
            "Consolidate first.",
        ),
        ("preset-overlap-bounce", "bounce-for-surface-rework", SCOPE, "Rework scope."),
        ("preset-settle-bounce", "bounce-for-surface-rework", SETTLEMENT, "Rework scope."),
        ("preset-queue-hold", "hold", QUEUE, "Hold until MR-1 and MR-2 resolve."),
    ],
)
def test_prohibited_cross_condition_tokens_become_stuck(
    tmp_path: Path, name: str, verdict: str, record: dict, note: str
):
    rules = ()
    if name == "preset-overlap-bounce":
        rules = ({"rule_id": "R-OVERLAP-SEQ", "outcome": "land-after-MR-2"},)
    elif name == "preset-settle-bounce":
        rules = ({"rule_id": "R-SETTLE-HOLD", "outcome": "hold"},)
    elif name == "preset-queue-hold":
        rules = ({"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"},)
    _, prepared, _, result = _run(
        tmp_path, name, _body(verdict, note), record=record, rules=rules
    )
    assert result.verdict == "escalate-stuck"
    assert result.error_kind == "verdict-condition-ineligible"
    assert _raw(prepared, result)["technical_error"] == result.error_kind


def test_exact_land_is_always_illegal_for_stage2(tmp_path: Path):
    _, prepared, _, result = _run(tmp_path, "land", _body("land"))
    assert result.verdict == "escalate-stuck"
    assert result.error_kind == "stage2-land-illegal"
    assert _raw(prepared, result)["technical_error"] == "stage2-land-illegal"


@pytest.mark.parametrize(
    ("name", "body", "error"),
    [
        ("malformed", "not JSON", "model-body-malformed"),
        (
            "extra-key",
            {**_body("bounce-for-surface-rework"), "extra": True},
            "model-body-fields-invalid",
        ),
        (
            "bad-anchor",
            _body(
                "bounce-for-surface-rework",
                observations=[{"text": "Synthetic.", "artifact_refs": ["packet/missing.json"]}],
            ),
            "model-observation-ref-invalid",
        ),
        ("empty-note", _body("bounce-for-surface-rework", ""), "model-note-empty"),
        (
            "unanchored",
            _body(
                "bounce-for-surface-rework",
                observations=[{"text": "Synthetic.", "artifact_refs": []}],
            ),
            "model-observation-unanchored",
        ),
    ],
)
def test_malformed_extra_key_bad_anchor_and_empty_note_are_evidenced_stuck(
    tmp_path: Path, name: str, body, error: str
):
    _, prepared, _, result = _run(tmp_path, name, body)
    assert result.verdict == "escalate-stuck"
    assert result.error_kind == error
    assert _raw(prepared, result)["technical_error"] == error


def _claude_envelope(body: dict, *, models=None, session="real-session-1") -> bytes:
    return _canonical(
        {
            "is_error": False,
            "subtype": "success",
            "result": json.dumps(body),
            "modelUsage": {"claude-opus-observed": {}} if models is None else models,
            "session_id": session,
        }
    )


def _real_result(stdout: bytes, *, stderr=b"", return_code=0, error=None, detail=None):
    return GenerationResult(
        mechanism="claude-p",
        stdout=stdout,
        stderr=stderr,
        return_code=return_code,
        technical_error=error,
        technical_detail=detail,
    )


def test_real_envelope_records_requested_alias_and_observed_identity_separately(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "identity")
    generated = _real_result(_claude_envelope(_body("bounce-for-surface-rework")))
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    assert result.generator == {
        "mechanism": "claude-p",
        "status": "success",
        "requested_model": "opus",
        "actual_model": "claude-opus-observed",
        "session_ref": "real-session-1",
        "error": None,
    }
    assert result.error_kind is None
    assert _raw(prepared, result)["technical_error"] is None
    assert _reconstruct(prepared, result, SURFACE)["verdict"] == result.verdict


def test_pre_generator_failure_reconstructs_exact_stuck_decision(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "pre-generator-reconstruction")
    result = prepare_technical_failure(
        prepared,
        root,
        Stage2Error("packet-invalid", "synthetic packet evidence is unusable"),
    )
    reconstructed = _reconstruct(prepared, result, SURFACE)
    assert reconstructed == {
        "generator": result.generator,
        "verdict": "escalate-stuck",
        "note": (
            "Stage-2 preparation failed: packet-invalid; "
            "synthetic packet evidence is unusable"
        ),
        "observations": [],
        "error_kind": "packet-invalid",
    }


@pytest.mark.parametrize(
    ("name", "generated", "error", "actual", "session"),
    [
        (
            "zero-models",
            _real_result(_claude_envelope(_body("bounce-for-surface-rework"), models={})),
            "model-identity-invalid",
            None,
            "real-session-1",
        ),
        (
            "multiple-models",
            _real_result(
                _claude_envelope(
                    _body("bounce-for-surface-rework"),
                    models={"model-a": {}, "model-b": {}},
                )
            ),
            "model-identity-invalid",
            None,
            "real-session-1",
        ),
        (
            "missing-session",
            _real_result(_claude_envelope(_body("bounce-for-surface-rework"), session="")),
            "session-identity-missing",
            "claude-opus-observed",
            None,
        ),
        (
            "empty-stdout",
            _real_result(b""),
            "empty-stdout",
            None,
            None,
        ),
        (
            "malformed-envelope",
            _real_result(b"{bad"),
            "claude-envelope-malformed",
            None,
            None,
        ),
        (
            "timeout",
            _real_result(b"partial", error="generator-timeout", detail="timed out", return_code=None),
            "generator-timeout",
            None,
            None,
        ),
        (
            "nonzero",
            _real_result(b"", stderr=b"synthetic error", return_code=7),
            "nonzero-exit",
            None,
            None,
        ),
        (
            "spawn",
            _real_result(b"", error="spawn-failure", detail="could not spawn", return_code=None),
            "spawn-failure",
            None,
            None,
        ),
    ],
)
def test_every_real_envelope_and_process_failure_has_truthful_provenance_and_raw_evidence(
    tmp_path: Path,
    name: str,
    generated: GenerationResult,
    error: str,
    actual: str | None,
    session: str | None,
):
    root, prepared = _prepared(tmp_path, name)
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    assert result.verdict == "escalate-stuck"
    assert result.error_kind == error
    assert result.generator["mechanism"] == "claude-p"
    assert result.generator["status"] == "technical-failure"
    assert result.generator["requested_model"] == REQUESTED_MODEL
    assert result.generator["actual_model"] == actual
    assert result.generator["session_ref"] == session
    assert result.generator["error"]["kind"] == error
    envelope = _raw(prepared, result)
    assert envelope["technical_error"] == error
    assert envelope["return_code"] == generated.return_code
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == error


def test_raw_envelope_bounds_and_sanitizes_stdout_stderr_and_details(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "bounded")
    generated = _real_result(
        (str(root).encode() + b"x" * (40 * 1024)),
        stderr=str(root).encode() + b"y" * (20 * 1024),
        return_code=9,
    )
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    envelope = _raw(prepared, result)
    assert str(root) not in json.dumps(envelope)
    assert "<research-root>" in envelope["stdout"]
    assert "<research-root>" in envelope["stderr"]
    assert envelope["stdout_truncated"] is True
    assert envelope["stderr_truncated"] is True
    assert result.error_kind == "generator-output-truncated"
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == result.error_kind


@pytest.mark.parametrize("path_kind", ["research-root", "detached-packet", "temporary-home"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_raw_redaction_is_safe_when_protected_path_crosses_truncation_boundary(
    tmp_path: Path,
    path_kind: str,
    stream: str,
):
    root, prepared = _prepared(tmp_path, f"boundary-{path_kind}-{stream}")
    generator = BoundaryPathGenerator(path_kind, stream)
    result = run_stage2(prepared, root, SURFACE, (), generator)
    envelope = _raw(prepared, result)
    protected = generator.protected_path
    assert protected is not None
    leaked_prefix = protected[: max(8, len(protected) // 2)]
    assert leaked_prefix not in envelope[stream]
    assert f"<{path_kind}>" in envelope[stream]
    assert envelope[f"{stream}_truncated"] is True
    assert envelope["generation"]["stdout_utf8"] is True
    assert result.error_kind == "generator-output-truncated"
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == result.error_kind


def test_multibyte_generator_error_uses_one_canonical_bounded_message(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "multibyte-detail")
    generated = _real_result(
        b"",
        error="sandbox-probe-failure",
        detail="é" * 1024,
        return_code=None,
    )
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    envelope = _raw(prepared, result)
    message = result.generator["error"]["message"]
    assert len(message.encode("utf-8")) <= 1024
    assert envelope["generation"]["technical_detail"] == message
    assert envelope["technical_detail"] == message
    assert _reconstruct(prepared, result, SURFACE) == {
        "generator": result.generator,
        "verdict": result.verdict,
        "note": result.note,
        "observations": [],
        "error_kind": result.error_kind,
    }


def test_whitespace_rich_multibyte_generator_failure_is_reconstructible(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "multibyte-whitespace-detail")
    detail = str(root.resolve()) + "\n\t" + ("é " * 800)
    generated = _real_result(
        b"",
        error="sandbox-probe-failure",
        detail=detail,
        return_code=None,
    )
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    envelope = _raw(prepared, result)
    message = result.generator["error"]["message"]
    assert len(message.encode("utf-8")) <= 1024
    assert envelope["generation"]["technical_detail"] == message
    assert envelope["technical_detail"] == message
    assert _reconstruct(prepared, result, SURFACE) == {
        "generator": result.generator,
        "verdict": result.verdict,
        "note": result.note,
        "observations": [],
        "error_kind": result.error_kind,
    }


@pytest.mark.parametrize(
    ("raw", "replacements"),
    [
        ("a" * 1023 + " b", {}),
        ("é " * 800, {}),
        (("alpha\n\t beta  " * 100), {}),
        (("<research-root> " * 100), {}),
        (("/private/synthetic-root\n\t" * 80), {"/private/synthetic-root": "<research-root>"}),
        ("", {}),
        (" \n\t ", {}),
    ],
    ids=[
        "ascii-boundary",
        "multibyte-boundary",
        "whitespace-rich",
        "redaction-sentinel",
        "protected-path",
        "empty-fallback",
        "whitespace-fallback",
    ],
)
def test_canonical_error_detail_is_idempotent_at_utf8_byte_boundaries(
    raw: str,
    replacements: dict[str, str],
):
    canonical = stage2_module._sanitized_error_detail(raw, replacements)
    assert len(canonical.encode("utf-8")) <= 1024
    assert stage2_module._sanitized_error_detail(canonical, replacements) == canonical
    for protected in replacements:
        assert protected not in canonical


@pytest.mark.parametrize(
    "sentinel",
    ["<research-root>", "<detached-packet>", "<temporary-home>"],
)
def test_literal_redaction_sentinel_in_model_body_is_rejected_consistently(
    tmp_path: Path,
    sentinel: str,
):
    root, prepared = _prepared(tmp_path, f"literal-{sentinel[1:-1]}")
    generated = GenerationResult.synthetic(
        _body("bounce-for-surface-rework", f"Synthetic unsafe literal {sentinel}."),
    )
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    assert result.error_kind == "model-body-unsafe"
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == result.error_kind


@pytest.mark.parametrize(
    "sentinel",
    ["<research-root>", "<detached-packet>", "<temporary-home>"],
)
def test_literal_redaction_sentinel_in_generator_identity_is_rejected_consistently(
    tmp_path: Path,
    sentinel: str,
):
    root, prepared = _prepared(tmp_path, f"literal-identity-{sentinel[1:-1]}")
    generated = _real_result(
        _claude_envelope(
            _body("bounce-for-surface-rework"),
            models={sentinel: {}},
        )
    )
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    assert result.error_kind == "generator-identity-unsafe"
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == result.error_kind


def test_post_call_invalid_generator_result_is_truthfully_generator_phase(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "invalid-result-object")
    generator = InvalidResultGenerator()
    result = run_stage2(prepared, root, SURFACE, (), generator)
    envelope = _raw(prepared, result)
    assert generator.calls == 1
    assert envelope["generation"]["phase"] == "generator"
    assert envelope["generation"]["mechanism"] == "claude-p"
    assert envelope["generation"]["technical_error"] == "generator-result-invalid"
    assert result.error_kind == "generator-result-invalid"
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == result.error_kind


def test_valid_large_model_body_becomes_reconstructible_truncation_failure(
    tmp_path: Path,
):
    observations = [
        {
            "text": f"Synthetic bounded observation {ordinal}: " + "x" * 12000,
            "artifact_refs": ["packet/001-report.md"],
        }
        for ordinal in range(3)
    ]
    _, prepared, _, result = _run(
        tmp_path,
        "valid-large-body",
        _body("bounce-for-surface-rework", observations=observations),
    )
    raw = _raw(prepared, result)
    assert raw["stdout_truncated"] is True
    assert result.error_kind == "generator-output-truncated"
    assert result.verdict == "escalate-stuck"
    assert _reconstruct(prepared, result, SURFACE) == {
        "generator": result.generator,
        "verdict": result.verdict,
        "note": result.note,
        "observations": [],
        "error_kind": result.error_kind,
    }


def test_generator_error_detail_is_bounded_and_research_root_sanitized(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "sanitized-detail")
    detail = str(root) + " " + ("sensitive-noise " * 200)
    generated = _real_result(
        b"",
        error="sandbox-probe-failure",
        detail=detail,
        return_code=None,
    )
    result = run_stage2(prepared, root, SURFACE, (), StaticGenerator(generated))
    message = result.generator["error"]["message"]
    assert str(root) not in message
    assert "<research-root>" in message
    assert len(message) <= 1024


def test_model_body_with_research_root_path_is_rejected_and_raw_copy_is_sanitized(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "unsafe-body")
    body = _body(
        "bounce-for-surface-rework",
        f"Rework the synthetic surface at {root}.",
    )
    result = run_stage2(
        prepared,
        root,
        SURFACE,
        (),
        StaticGenerator(GenerationResult.synthetic(body)),
    )
    assert result.error_kind == "model-body-unsafe"
    assert str(root) not in result.note
    assert str(root) not in json.dumps(json.loads((prepared.attempt_dir / "decision.json").read_text()))
    assert str(root) not in json.dumps(_raw(prepared, result))
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == "model-body-unsafe"


def test_sanitized_generator_identity_and_invalid_utf8_are_reconstructible(
    tmp_path: Path,
):
    root, prepared = _prepared(tmp_path, "unsafe-generator-identity")
    unsafe_identity = _real_result(
        _claude_envelope(
            _body("bounce-for-surface-rework"), models={str(root): {}}
        )
    )
    result = run_stage2(
        prepared, root, SURFACE, (), StaticGenerator(unsafe_identity)
    )
    assert result.error_kind == "generator-identity-unsafe"
    assert _reconstruct(prepared, result, SURFACE)["error_kind"] == result.error_kind

    root2, prepared2 = _prepared(tmp_path, "invalid-utf8")
    invalid_utf8 = GenerationResult(
        mechanism="injected-synthetic",
        stdout=b"\xff",
        synthetic_session_id="synthetic-invalid-utf8",
    )
    result2 = run_stage2(
        prepared2, root2, SURFACE, (), StaticGenerator(invalid_utf8)
    )
    assert result2.error_kind == "model-body-malformed"
    assert _raw(prepared2, result2)["generation"]["stdout_utf8"] is False
    assert _reconstruct(prepared2, result2, SURFACE)["error_kind"] == result2.error_kind


def test_detached_copy_matches_every_manifest_byte_and_uses_private_external_roots(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "detach")
    detached = detach_packet(prepared, root)
    try:
        assert not detached.root.is_relative_to(root)
        assert not detached.home_dir.is_relative_to(root)
        assert detached.home_dir != detached.root
        assert (detached.root.stat().st_mode & 0o777) == 0o700
        assert (detached.home_dir.stat().st_mode & 0o777) == 0o700
        for ref, digest in prepared.input_hashes.items():
            source = prepared.attempt_dir / ref
            copied = detached.root / ref
            assert copied.read_bytes() == source.read_bytes()
            assert hashlib.sha256(copied.read_bytes()).hexdigest() == digest
        assert (detached.packet_dir / "manifest.json").read_bytes() == (
            prepared.packet_dir / "manifest.json"
        ).read_bytes()
    finally:
        cleanup_detached(detached)
    assert not detached.root.exists() and not detached.home_dir.exists()


@pytest.mark.parametrize("mutation", ["artifact", "missing", "manifest", "extra", "symlink"])
def test_tampering_missing_hash_or_extra_packet_path_fails_closed_with_raw_evidence(
    tmp_path: Path, mutation: str
):
    root, prepared = _prepared(tmp_path, f"tamper-{mutation}")
    if mutation == "artifact":
        (prepared.packet_dir / "001-report.md").write_bytes(b"tampered\n")
        expected = "detached-artifact-mismatch"
    elif mutation == "missing":
        (prepared.packet_dir / "001-report.md").unlink()
        expected = "detached-extra-path"
    elif mutation == "manifest":
        (prepared.packet_dir / "manifest.json").write_bytes(b"{}\n")
        expected = "detached-manifest-mismatch"
    elif mutation == "extra":
        (prepared.packet_dir / "extra.txt").write_text("extra\n")
        expected = "detached-extra-path"
    else:
        (prepared.packet_dir / "unsafe-link").symlink_to("context.json")
        expected = "detached-path-unsafe"
    generator = StaticGenerator(GenerationResult.synthetic(_body("bounce-for-surface-rework")))
    result = run_stage2(prepared, root, SURFACE, (), generator)
    assert result.verdict == "escalate-stuck"
    assert result.error_kind == expected
    assert generator.calls == 0
    assert _raw(prepared, result)["technical_error"] == expected


def test_unsafe_manifest_artifact_ref_fails_before_copy_and_is_evidenced(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "unsafe-manifest-ref")
    manifest_path = prepared.packet_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    old_ref = manifest["artifacts"][0]["packet_ref"]
    unsafe_ref = "packet//context.json"
    manifest["artifacts"][0]["packet_ref"] = unsafe_ref
    manifest["artifact_refs"][0] = unsafe_ref
    manifest["input_hashes"][unsafe_ref] = manifest["input_hashes"].pop(old_ref)
    manifest["source_refs"][unsafe_ref] = manifest["source_refs"].pop(old_ref)
    manifest_bytes = _canonical(manifest)
    manifest_path.write_bytes(manifest_bytes)
    prepared = replace(
        prepared,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artifact_refs=tuple(manifest["artifact_refs"]),
        input_hashes=manifest["input_hashes"],
    )
    result = run_stage2(
        prepared,
        root,
        SURFACE,
        (),
        StaticGenerator(GenerationResult.synthetic(_body("bounce-for-surface-rework"))),
    )
    assert result.error_kind == "detached-manifest-invalid"
    assert _raw(prepared, result)["technical_error"] == "detached-manifest-invalid"


def test_invalid_generator_result_and_temp_factory_exception_still_write_raw_evidence(
    tmp_path: Path,
):
    root, prepared = _prepared(tmp_path, "invalid-generator-result")

    class InvalidGenerator:
        def generate(self, request):
            return None

    result = run_stage2(prepared, root, SURFACE, (), InvalidGenerator())
    assert result.error_kind == "generator-result-invalid"
    assert _raw(prepared, result)["technical_error"] == "generator-result-invalid"

    root2, prepared2 = _prepared(tmp_path, "temp-failure")
    calls = 0

    def failing_temp_factory(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic temp failure")
        path = tmp_path / "orphan-check"
        path.mkdir()
        return str(path)

    result2 = run_stage2(
        prepared2,
        root2,
        SURFACE,
        (),
        StaticGenerator(GenerationResult.synthetic(_body("bounce-for-surface-rework"))),
        temp_factory=failing_temp_factory,
    )
    assert result2.error_kind == "preparation-exception"
    assert _raw(prepared2, result2)["technical_error"] == "preparation-exception"
    assert not (tmp_path / "orphan-check").exists()


class FakeSandbox:
    def __init__(self, *, error: SandboxError | None = None):
        self.error = error
        self.prepared = False
        self.probed = False

    def prepare(self, *, detached, research_root, executable):
        self.prepared = True
        if self.error and self.error.kind != "sandbox-probe-failure":
            raise self.error
        return SandboxLaunch(("fake-sandbox",), "fake-policy")

    def probe(self, launch, *, detached, research_root, environment):
        self.probed = True
        assert (detached.packet_dir / "manifest.json").is_file()
        assert not any(str(research_root) in value for value in environment.values())
        if self.error:
            raise self.error


def test_claude_driver_uses_probe_isolated_home_exact_tools_and_no_repo_add_dir(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "driver")
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    sandbox = FakeSandbox()
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_claude_envelope(_body("bounce-for-surface-rework")),
            stderr=b"",
        )

    driver = ClaudeGenerator(sandbox=sandbox, binary=executable, run=fake_run)
    result = run_stage2(prepared, root, SURFACE, (), driver)
    assert result.error_kind is None
    assert sandbox.prepared and sandbox.probed
    command = observed["command"]
    assert command[0] == "fake-sandbox"
    assert command[1:4] == [str(executable.resolve()), "-p", command[3]]
    tools_index = command.index("--allowedTools")
    assert tuple(command[tools_index + 1 : tools_index + 4]) == ALLOWED_TOOLS
    assert command[tools_index + 4 : tools_index + 6] == ["--model", "opus"]
    assert "--add-dir" not in command
    assert observed["cwd"] != root
    assert observed["env"]["HOME"] != str(Path.home())
    assert str(root) not in json.dumps(observed["env"])


@pytest.mark.parametrize(
    "error",
    [
        SandboxError("sandbox-unavailable", "sandbox executable missing"),
        SandboxError("sandbox-probe-failure", "filesystem semantics not enforced"),
    ],
)
def test_sandbox_configuration_or_probe_failure_never_falls_back_to_process(
    tmp_path: Path, error: SandboxError
):
    root, prepared = _prepared(tmp_path, error.kind)
    executable = tmp_path / f"claude-{error.kind}"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    sandbox = FakeSandbox(error=error)
    calls = []

    def forbidden_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unsandboxed subprocess fallback")

    result = run_stage2(
        prepared,
        root,
        SURFACE,
        (),
        ClaudeGenerator(sandbox=sandbox, binary=executable, run=forbidden_run),
    )
    assert result.verdict == "escalate-stuck"
    assert result.error_kind == error.kind
    assert calls == []
    assert _raw(prepared, result)["technical_error"] == error.kind


def test_unsupported_platform_fails_closed_without_asserting_macos_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, prepared = _prepared(tmp_path, "unsupported")
    executable = tmp_path / "claude-unsupported"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    monkeypatch.setattr("composition_gate.stage2.platform.system", lambda: "Linux")
    calls = []

    def forbidden_run(*args, **kwargs):
        calls.append(True)
        raise AssertionError("must not run")

    result = run_stage2(
        prepared,
        root,
        SURFACE,
        (),
        ClaudeGenerator(binary=executable, run=forbidden_run),
    )
    assert result.error_kind == "sandbox-unsupported"
    assert calls == []


def test_macos_policy_is_explicit_allowlist_with_network_and_research_root_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, prepared = _prepared(tmp_path, "policy")
    detached = detach_packet(prepared, root)
    sandbox_binary = tmp_path / "sandbox-exec"
    sandbox_binary.write_text("#!/bin/sh\nexit 1\n")
    sandbox_binary.chmod(0o700)
    executable = tmp_path / "claude-policy"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    monkeypatch.setattr("composition_gate.stage2.platform.system", lambda: "Darwin")
    try:
        launch = MacOSSandbox(sandbox_binary=sandbox_binary).prepare(
            detached=detached,
            research_root=root.resolve(),
            executable=executable.resolve(),
        )
        assert "(deny default)" in launch.policy
        assert '(import "system.sb")' in launch.policy
        assert "(allow network*)" in launch.policy
        assert str(detached.root) in launch.policy
        assert str(detached.home_dir) in launch.policy
        assert f'(deny file-read* file-write* (subpath "{root.resolve()}"))' in launch.policy
    finally:
        cleanup_detached(detached)


def test_macos_direct_probe_checks_packet_parent_and_absolute_root_read_write_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, prepared = _prepared(tmp_path, "probe-semantics")
    (root / ".git").write_text("gitdir: synthetic-only\n")
    detached = detach_packet(prepared, root)
    sandbox_binary = tmp_path / "sandbox-probe"
    sandbox_binary.write_text("#!/bin/sh\nexit 1\n")
    sandbox_binary.chmod(0o700)
    executable = tmp_path / "claude-probe"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    observed = {}

    def fake_probe_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("composition_gate.stage2.platform.system", lambda: "Darwin")
    sandbox = MacOSSandbox(sandbox_binary=sandbox_binary, run=fake_probe_run)
    try:
        launch = sandbox.prepare(
            detached=detached,
            research_root=root.resolve(),
            executable=executable.resolve(),
        )
        environment = child_environment(detached, root.resolve())
        sandbox.probe(
            launch,
            detached=detached,
            research_root=root.resolve(),
            environment=environment,
        )
        script = observed["command"][-1]
        assert "test -r packet/manifest.json" in script
        assert "cat ../" in script and "parent-access-must-fail" in script
        assert str((root / ".git").resolve()) in script
        assert "touch" in script and str(root.resolve()) in script
        assert observed["cwd"] == detached.root
        assert not any(str(root.resolve()) in value for value in observed["env"].values())
    finally:
        cleanup_detached(detached)


def test_attempt_raw_output_collision_never_overwrites_first_evidence(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "collision")
    first_result = GenerationResult.synthetic(_body("bounce-for-surface-rework"))
    first = write_raw_output(prepared, root, first_result, None)
    path = prepared.attempt_dir / "raw-output.json"
    original = path.read_bytes()
    with pytest.raises(Stage2Error, match="overwrite") as caught:
        write_raw_output(prepared, root, first_result, "synthetic-second-write")
    assert caught.value.kind == "evidence-collision"
    assert path.read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == first.sha256


def test_attempt_decision_collision_never_overwrites_first_evidence(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "decision-collision")
    packet = {
        "manifest_ref": prepared.manifest_ref,
        "manifest_sha256": prepared.manifest_sha256,
        "artifact_refs": list(prepared.artifact_refs),
        "input_hashes": prepared.input_hashes,
    }
    template = {"name": "composition-gate-review.v1.md", "sha256": "0" * 64}
    generator = {
        "mechanism": "injected-synthetic",
        "status": "synthetic",
        "requested_model": None,
        "actual_model": "<synthetic>",
        "session_ref": "synthetic-session",
        "error": None,
    }
    raw = write_raw_output(
        prepared,
        root,
        GenerationResult.synthetic(_body("bounce-for-surface-rework")),
        None,
    )
    decision = ValidatedDecision("bounce-for-surface-rework", "Synthetic.", ())
    first = write_decision_output(
        prepared,
        root,
        packet=packet,
        template=template,
        generator=generator,
        decision=decision,
        raw_output=raw,
        error_kind=None,
    )
    path = prepared.attempt_dir / "decision.json"
    original = path.read_bytes()
    with pytest.raises(Stage2Error, match="overwrite") as caught:
        write_decision_output(
            prepared,
            root,
            packet=packet,
            template=template,
            generator=generator,
            decision=decision,
            raw_output=raw,
            error_kind=None,
        )
    assert caught.value.kind == "evidence-collision"
    assert path.read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == first.sha256


def test_stage2_changes_no_packet_bytes_and_only_adds_attempt_raw_evidence(tmp_path: Path):
    root, prepared = _prepared(tmp_path, "no-root-mutation")
    before = {
        path.relative_to(prepared.attempt_dir).as_posix(): path.read_bytes()
        for path in prepared.packet_dir.rglob("*")
        if path.is_file()
    }
    before_tree = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )
    result = run_stage2(
        prepared,
        root,
        SURFACE,
        (),
        StaticGenerator(GenerationResult.synthetic(_body("bounce-for-surface-rework"))),
    )
    after = {
        path.relative_to(prepared.attempt_dir).as_posix(): path.read_bytes()
        for path in prepared.packet_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    after_tree = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )
    assert after_tree == sorted(
        before_tree
        + [
            f"var/cgate/MR-1/attempts/{ATTEMPT}/decision.json",
            f"var/cgate/MR-1/attempts/{ATTEMPT}/raw-output.json",
        ]
    )
    assert _raw(prepared, result)["technical_error"] is None


def test_tests_import_the_current_source_tree_not_an_installed_wheel():
    import composition_gate.stage2 as stage2

    assert Path(stage2.__file__).resolve() == (
        PROJECT_ROOT
        / "system/instruments/composition-gate/composition_gate/stage2.py"
    ).resolve()
