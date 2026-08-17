"""Root-only composition-gate finalization.

All expensive preparation belongs to ``composition_gate``.  This module is the
small Tier-1 critical section: revalidate one prepared decision, allocate IDs,
construct one compound Plan, and let the shared pipeline commit it once.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

from composition_gate.decision import DecisionError, prepare_decision
from composition_gate.packet import (
    MANIFEST_FORMAT,
    PROMPT_NAME,
    PROMPT_SHA256,
    DecisionSnapshot,
    PacketError,
    PreparedPacket,
    SnapshotReader,
    review_prompt_bytes,
)
from composition_gate.stage2 import (
    DECISION_FORMAT,
    RAW_OUTPUT_FORMAT,
    Stage2Error,
    verify_stage2_raw_evidence,
)

from .. import gitutil, jsonio
from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan
from ._common import Ctx, today


FINALIZATION_FORMAT = "composition-gate-finalization.v1"
_RECORD_ID = re.compile(r"MR-(0|[1-9][0-9]*)")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
_GR_FILE = re.compile(r"GR-(0|[1-9][0-9]*)\.json")
_RQ_FILE = re.compile(r"RQ-(0|[1-9][0-9]*)\.json")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STAGE2_KEYS = {
    "attempt_id",
    "packet",
    "template",
    "generator",
    "verdict",
    "note",
    "observations",
    "raw_output",
    "decision_output",
    "error_kind",
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _strict_json(content: bytes, label: str) -> Any:
    try:
        return json.loads(
            content.decode("utf-8", errors="strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HtError(f"cgate finalizer rejected malformed {label}: {exc}") from exc


def _canonical_bytes(value: Any) -> bytes:
    try:
        return jsonio.dumps(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HtError(f"cgate finalizer cannot canonicalize prepared evidence: {exc}") from exc


def _relative_parts(ref: Any, *, prefix: str) -> tuple[str, ...]:
    if not isinstance(ref, str) or not ref or "\\" in ref:
        raise HtError(f"cgate finalizer rejected unsafe ref {ref!r}")
    path = PurePosixPath(ref)
    parts = path.parts
    if (
        path.is_absolute()
        or path.as_posix() != ref
        or any(part in {"", ".", ".."} for part in parts)
        or not ref.startswith(prefix)
    ):
        raise HtError(f"cgate finalizer rejected unsafe ref {ref!r}")
    return parts


def _stable_relative_file(root: Path, ref: Any, *, prefix: str) -> bytes:
    """Read a regular in-root file without following any symlink component."""

    parts = _relative_parts(ref, prefix=prefix)
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise HtError(f"cgate finalizer cannot read fingerprinted ref {ref!r}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise HtError(f"cgate finalizer rejected symlink/path swap at {ref!r}")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise HtError(f"cgate finalizer rejected non-directory path component at {ref!r}")
        if index == len(parts) - 1 and not stat.S_ISREG(metadata.st_mode):
            raise HtError(f"cgate finalizer rejected non-regular evidence at {ref!r}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(current, flags)
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise HtError(f"cgate finalizer cannot stably read {ref!r}: {exc}") from exc
    fingerprint = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    content = b"".join(chunks)
    if fingerprint(before) != fingerprint(after) or len(content) != after.st_size:
        raise HtError(f"cgate finalizer evidence changed while reading {ref!r}")
    try:
        final = current.lstat()
    except OSError as exc:
        raise HtError(f"cgate finalizer evidence changed after reading {ref!r}: {exc}") from exc
    if fingerprint(after) != fingerprint(final):
        raise HtError(f"cgate finalizer evidence path changed while reading {ref!r}")
    return content


def _clean_index(root: Path) -> None:
    result = gitutil.run(root, ["diff", "--cached", "--quiet"], check=False)
    if result.returncode == 1:
        raise HtError("cgate finalizer requires a clean Git index")
    if result.returncode != 0:
        raise HtError("cgate finalizer could not verify the Git index")


def _validate_stage2(
    root: Path,
    record_id: str,
    decision: Mapping[str, Any],
    prepared: Any,
    snapshot: DecisionSnapshot,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(prepared, dict) or set(prepared) != _STAGE2_KEYS:
        raise HtError("cgate finalizer rejected malformed stage-2 preparation")
    attempt_id = prepared.get("attempt_id")
    if not isinstance(attempt_id, str) or _ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise HtError("cgate finalizer rejected malformed stage-2 attempt id")
    packet = prepared.get("packet")
    template = prepared.get("template")
    generator = prepared.get("generator")
    raw_output = prepared.get("raw_output")
    decision_output = prepared.get("decision_output")
    observations = prepared.get("observations")
    if not all(isinstance(value, dict) for value in (packet, template, generator, raw_output, decision_output)):
        raise HtError("cgate finalizer rejected incomplete stage-2 metadata")
    if not isinstance(observations, list):
        raise HtError("cgate finalizer rejected malformed stage-2 observations")

    attempt_prefix = f"var/cgate/{record_id}/attempts/{attempt_id}/"
    manifest_ref = packet.get("manifest_ref")
    if manifest_ref != attempt_prefix + "packet/manifest.json":
        raise HtError("cgate finalizer rejected stage-2 manifest identity mismatch")
    manifest_bytes = _stable_relative_file(root, manifest_ref, prefix=attempt_prefix)
    if hashlib.sha256(manifest_bytes).hexdigest() != packet.get("manifest_sha256"):
        raise HtError("cgate finalizer rejected changed stage-2 manifest")
    manifest = _strict_json(manifest_bytes, "stage-2 manifest")
    manifest_fields = {
        "format", "attempt_id", "record_id", "snapshot", "artifacts",
        "artifact_refs", "input_hashes", "source_refs",
    }
    if not isinstance(manifest, dict) or set(manifest) != manifest_fields:
        raise HtError("cgate finalizer rejected malformed stage-2 manifest fields")
    if (
        manifest.get("format") != MANIFEST_FORMAT
        or manifest.get("attempt_id") != attempt_id
        or manifest.get("record_id") != record_id
        or manifest.get("snapshot") != {
            "head_commit": snapshot.head_commit,
            "head_tree": snapshot.head_tree,
        }
    ):
        raise HtError("cgate finalizer rejected stale stage-2 manifest identity")
    if _canonical_bytes(manifest) != manifest_bytes:
        raise HtError("cgate finalizer rejected non-canonical stage-2 manifest bytes")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise HtError("cgate finalizer rejected empty stage-2 manifest")
    hashes: dict[str, str] = {}
    sources: dict[str, str] = {}
    reader = SnapshotReader(snapshot)
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {
            "source_ref", "git_oid", "packet_ref", "artifact_kind", "sha256"
        }:
            raise HtError("cgate finalizer rejected malformed packet artifact row")
        ref = row.get("packet_ref")
        if not isinstance(ref, str) or not ref.startswith("packet/") or ref == "packet/manifest.json":
            raise HtError("cgate finalizer rejected unsafe packet artifact ref")
        _relative_parts(ref, prefix="packet/")
        digest = row.get("sha256")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None or ref in hashes:
            raise HtError("cgate finalizer rejected duplicate or malformed packet hash")
        local = _stable_relative_file(root, attempt_prefix + ref, prefix=attempt_prefix)
        if hashlib.sha256(local).hexdigest() != digest:
            raise HtError(f"cgate finalizer rejected changed packet artifact {ref}")
        hashes[ref] = digest
        source_ref = row.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise HtError("cgate finalizer rejected malformed packet source ref")
        sources[ref] = source_ref
        if source_ref.startswith("generated:"):
            if row.get("git_oid") is not None:
                raise HtError("cgate finalizer rejected generated packet Git identity")
            if row.get("artifact_kind") == "packet-context":
                context = _strict_json(local, "packet context")
                if (
                    not isinstance(context, dict)
                    or context.get("format") != "composition-gate-packet.v1"
                    or context.get("snapshot") != {
                        "head_commit": snapshot.head_commit,
                        "head_tree": snapshot.head_tree,
                    }
                ):
                    raise HtError("cgate finalizer rejected stale packet context")
                if source_ref == "generated:packet-context":
                    if context.get("merge_record") != record:
                        raise HtError("cgate finalizer rejected changed packet MR context")
                elif source_ref == "generated:packet-preparation-error":
                    if (
                        context.get("record_id") != record_id
                        or context.get("packet_status") != "technical-failure"
                        or not isinstance(context.get("error"), dict)
                    ):
                        raise HtError("cgate finalizer rejected malformed packet-failure context")
                else:
                    raise HtError("cgate finalizer rejected unknown generated packet context")
        else:
            try:
                source = reader.read(source_ref)
            except PacketError as exc:
                raise HtError(
                    f"cgate finalizer cannot re-read packet source [{exc.kind}]: {exc.message}"
                ) from exc
            if source.git_oid != row.get("git_oid") or hashlib.sha256(source.content).hexdigest() != digest:
                raise HtError(f"cgate finalizer rejected stale packet source {source_ref}")

    if (
        manifest.get("artifact_refs") != list(hashes)
        or manifest.get("input_hashes") != hashes
        or manifest.get("source_refs") != sources
        or packet.get("artifact_refs") != list(hashes)
        or packet.get("input_hashes") != hashes
    ):
        raise HtError("cgate finalizer rejected inconsistent packet manifest metadata")

    packet_dir = root / attempt_prefix / "packet"
    observed_files: set[str] = set()
    try:
        for directory, dirnames, filenames in os.walk(packet_dir, followlinks=False):
            parent = Path(directory)
            for name in (*dirnames, *filenames):
                if (parent / name).is_symlink():
                    raise HtError("cgate finalizer rejected packet symlink/path swap")
            for name in filenames:
                observed_files.add((parent / name).relative_to(packet_dir.parent).as_posix())
    except OSError as exc:
        raise HtError(f"cgate finalizer cannot enumerate packet evidence: {exc}") from exc
    if observed_files != set(hashes) | {"packet/manifest.json"}:
        raise HtError("cgate finalizer rejected extra or missing packet evidence")

    if template != {"name": PROMPT_NAME, "sha256": PROMPT_SHA256}:
        raise HtError("cgate finalizer rejected stage-2 template identity")
    try:
        prompt = review_prompt_bytes()
    except PacketError as exc:
        raise HtError(f"cgate finalizer rejected prompt [{exc.kind}]: {exc.message}") from exc
    if hashlib.sha256(prompt).hexdigest() != PROMPT_SHA256:
        raise HtError("cgate finalizer rejected changed stage-2 prompt")

    raw_ref = raw_output.get("ref")
    if raw_ref != attempt_prefix + "raw-output.json":
        raise HtError("cgate finalizer rejected raw-output identity mismatch")
    raw_bytes = _stable_relative_file(root, raw_ref, prefix=attempt_prefix)
    if hashlib.sha256(raw_bytes).hexdigest() != raw_output.get("sha256"):
        raise HtError("cgate finalizer rejected changed raw-output evidence")
    raw = _strict_json(raw_bytes, "stage-2 raw output")
    if not isinstance(raw, dict) or raw.get("format") != RAW_OUTPUT_FORMAT:
        raise HtError("cgate finalizer rejected malformed raw-output envelope")
    if _canonical_bytes(raw) != raw_bytes or raw.get("technical_error") != prepared.get("error_kind"):
        raise HtError("cgate finalizer rejected inconsistent raw-output evidence")

    reconstructed_packet = PreparedPacket(
        attempt_id=attempt_id,
        attempt_dir=root / attempt_prefix,
        packet_dir=root / attempt_prefix / "packet",
        manifest_ref=manifest_ref,
        manifest_sha256=packet["manifest_sha256"],
        artifact_refs=tuple(packet["artifact_refs"]),
        input_hashes=dict(packet["input_hashes"]),
    )
    try:
        reconstructed = verify_stage2_raw_evidence(
            raw,
            prepared=reconstructed_packet,
            record=record,
            rules_fired=decision.get("rules_fired", []),
            manifest=manifest,
        )
    except Stage2Error as exc:
        raise HtError(
            f"cgate finalizer rejected stage-2 reconstruction [{exc.kind}]: {exc.message}"
        ) from exc
    for field in ("generator", "verdict", "note", "observations", "error_kind"):
        if prepared.get(field) != reconstructed[field]:
            raise HtError(
                f"cgate finalizer rejected prepared stage-2 {field} divergence"
            )

    decision_ref = decision_output.get("ref")
    if decision_ref != attempt_prefix + "decision.json":
        raise HtError("cgate finalizer rejected decision evidence identity mismatch")
    decision_bytes = _stable_relative_file(root, decision_ref, prefix=attempt_prefix)
    if hashlib.sha256(decision_bytes).hexdigest() != decision_output.get("sha256"):
        raise HtError("cgate finalizer rejected changed stage-2 decision evidence")
    expected_decision = {
        "format": DECISION_FORMAT,
        "attempt_id": attempt_id,
        "packet": packet,
        "template": template,
        "generator": generator,
        "verdict": prepared.get("verdict"),
        "note": prepared.get("note"),
        "observations": observations,
        "raw_output": raw_output,
        "error_kind": prepared.get("error_kind"),
    }
    if decision_bytes != _canonical_bytes(expected_decision) or _strict_json(
        decision_bytes, "stage-2 decision"
    ) != expected_decision:
        raise HtError("cgate finalizer rejected inconsistent stage-2 decision bytes")
    if not isinstance(prepared.get("verdict"), str) or not isinstance(prepared.get("note"), str):
        raise HtError("cgate finalizer rejected incomplete stage-2 decision")
    return dict(prepared)


def _revalidate_payload(ctx: Ctx, payload: Any) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != {"format", "decision", "stage2"}:
        raise HtUsageError("cgate finalizer payload has the wrong fields")
    if payload.get("format") != FINALIZATION_FORMAT:
        raise HtUsageError("cgate finalizer payload has the wrong format")
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        raise HtUsageError("cgate finalizer payload has no decision")
    record_id = decision.get("record_id")
    if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
        raise HtUsageError("cgate finalizer payload has an invalid MR id")

    _clean_index(ctx.root.path)
    try:
        snapshot = DecisionSnapshot.capture(ctx.root.path)
    except PacketError as exc:
        raise HtError(f"cgate finalizer cannot capture current snapshot [{exc.kind}]: {exc.message}") from exc
    fingerprints = decision.get("fingerprints")
    if not isinstance(fingerprints, dict) or (
        fingerprints.get("decision_head") != snapshot.head_commit
        or fingerprints.get("decision_tree") != snapshot.head_tree
    ):
        raise HtError("cgate finalizer rejected stale physical HEAD/tree")

    record_ref = f"tier1/merge-records/{record_id}.json"
    try:
        source = SnapshotReader(snapshot).read(record_ref)
    except PacketError as exc:
        raise HtError(f"cgate finalizer cannot read committed MR [{exc.kind}]: {exc.message}") from exc
    record_fingerprint = fingerprints.get("merge_record")
    if record_fingerprint != {
        "ref": record_ref,
        "git_oid": source.git_oid,
        "sha256": hashlib.sha256(source.content).hexdigest(),
    }:
        raise HtError("cgate finalizer rejected stale committed MR bytes/OID/hash")
    worktree_bytes = _stable_relative_file(ctx.root.path, record_ref, prefix="tier1/merge-records/")
    if worktree_bytes != source.content:
        raise HtError("cgate finalizer refused a dirty or swapped MR worktree path")
    record = _strict_json(source.content, "committed merge record")
    if (
        not isinstance(record, dict)
        or record.get("id") != record_id
        or record.get("gate_verdict") is not None
        or record.get("consumed_epoch") is not None
    ):
        raise HtError("cgate finalizer rejected decided, consumed, or malformed MR")

    evidence = fingerprints.get("screen_evidence")
    if not isinstance(evidence, list) or len(evidence) != 2:
        raise HtError("cgate finalizer rejected malformed screen fingerprints")
    screen = record.get("screen") if isinstance(record.get("screen"), dict) else {}
    for kind, row in zip(("output", "log"), evidence, strict=True):
        if not isinstance(row, dict) or set(row) != {"kind", "ref", "sha256"} or row.get("kind") != kind:
            raise HtError("cgate finalizer rejected malformed screen fingerprint row")
        if row.get("ref") != screen.get(f"{kind}_ref"):
            raise HtError("cgate finalizer rejected changed screen evidence ref")
        if row.get("ref") is None:
            if row.get("sha256") is not None:
                raise HtError("cgate finalizer rejected incomplete null screen fingerprint")
            continue
        content = _stable_relative_file(ctx.root.path, row["ref"], prefix="var/")
        if hashlib.sha256(content).hexdigest() != row.get("sha256"):
            raise HtError("cgate finalizer rejected changed screen evidence bytes")

    try:
        recomputed = prepare_decision(ctx.root.path, record_id)
    except DecisionError as exc:
        raise HtError(f"cgate finalizer revalidation failed [{exc.kind}]: {exc.message}") from exc
    if recomputed != decision:
        raise HtError("cgate finalizer rejected changed prepared decision")

    stage2_prepared = payload.get("stage2")
    if decision.get("route") == "stage2":
        stage2_validated = _validate_stage2(
            ctx.root.path, record_id, decision, stage2_prepared, snapshot, record
        )
    else:
        if stage2_prepared is not None:
            raise HtError("cgate finalizer rejected stage-2 data on a stage-1 route")
        stage2_validated = None
    return decision, stage2_validated, record


def _next_id(
    root: Path,
    directory: Path,
    pattern: re.Pattern[str],
    prefix: str,
    history_prefix: str,
) -> str:
    ordinals: list[int] = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = pattern.fullmatch(path.name)
            if match is not None:
                ordinals.append(int(match.group(1)))
    history = gitutil.run(
        root,
        [
            "--no-replace-objects",
            "log",
            "--all",
            "--format=",
            "--name-only",
            "--",
            history_prefix,
        ],
        check=False,
    )
    if history.returncode != 0:
        raise HtError(f"cgate finalizer cannot inspect historical {prefix} paths")
    for raw_path in history.stdout.splitlines():
        if not raw_path.startswith(history_prefix + "/"):
            continue
        match = pattern.fullmatch(raw_path.removeprefix(history_prefix + "/"))
        if match is not None:
            ordinals.append(int(match.group(1)))
    return f"{prefix}-{max(ordinals, default=0) + 1}"


def build_finalization_plan(ctx: Ctx, payload: Any) -> tuple[Plan, dict[str, Any]]:
    """Build the single compound Plan.  The caller must hold the Tier-1 mutex."""

    if ctx.role != "cgate":
        raise HtError("cgate finalizer requires HT_ROLE=cgate")
    decision, stage2_prepared, record = _revalidate_payload(ctx, payload)
    record_id = decision["record_id"]
    verdict = (
        decision["verdict"] if stage2_prepared is None else stage2_prepared["verdict"]
    )
    note = decision["note"] if stage2_prepared is None else stage2_prepared["note"]
    escalated = verdict in {"escalate-to-user", "escalate-stuck"}

    review_id = _next_id(
        ctx.root.path,
        ctx.root.tier1_dir / "gate-reviews",
        _GR_FILE,
        "GR",
        "tier1/gate-reviews",
    )
    rq_id = (
        _next_id(
            ctx.root.path,
            ctx.root.tier1_dir / "ratification-queue",
            _RQ_FILE,
            "RQ",
            "tier1/ratification-queue",
        )
        if escalated
        else None
    )
    review_path = ctx.root.gate_review_json(review_id)
    if review_path.exists() or review_path.is_symlink():
        raise HtError(f"cgate finalizer review allocation collided at {review_id}")
    if rq_id is not None:
        rq_path = ctx.root.ratification_item_json(rq_id)
        if rq_path.exists() or rq_path.is_symlink():
            raise HtError(f"cgate finalizer RQ allocation collided at {rq_id}")

    created = today()
    review = {
        "id": review_id,
        "merge_record_ref": record_id,
        "created": created,
        "stage": decision["stage"],
        "attempt_id": None if stage2_prepared is None else stage2_prepared["attempt_id"],
        "screen_ref": decision["screen_ref"],
        "packet": None if stage2_prepared is None else stage2_prepared["packet"],
        "template": None if stage2_prepared is None else stage2_prepared["template"],
        "generator": None if stage2_prepared is None else stage2_prepared["generator"],
        "rules_fired": [] if stage2_prepared is None else decision["rules_fired"],
        "verdict": verdict,
        "note": note,
        "observations": [] if stage2_prepared is None else stage2_prepared["observations"],
        "raw_output": None if stage2_prepared is None else stage2_prepared["raw_output"],
        "escalation_ref": rq_id,
    }
    review_sha256 = hashlib.sha256(_canonical_bytes(review)).hexdigest()
    updated_record = dict(record)
    updated_record["gate_verdict"] = {
        "verdict": verdict,
        "date": created,
        "review_ref": review_id,
        "review_sha256": review_sha256,
        "note": note,
    }
    writes = [
        DocWrite(review_path, "gate_review", None, review),
        DocWrite(
            ctx.root.merge_record_json(record_id),
            "merge_record",
            record,
            updated_record,
        ),
    ]
    rq_document: dict[str, Any] | None = None
    if rq_id is not None:
        rq_document = {
            "id": rq_id,
            "kind": "cgate-escalation",
            "payload_ref": f"tier1/gate-reviews/{review_id}.json",
            "text": note,
            "queued_by": "cgate",
            "date": created,
            "disposition": None,
            "annotations": [],
        }
        writes.append(
            DocWrite(
                ctx.root.ratification_item_json(rq_id),
                "ratification_item",
                None,
                rq_document,
            )
        )

    def semantic() -> None:
        _revalidate_payload(ctx, payload)
        if hashlib.sha256(_canonical_bytes(review)).hexdigest() != updated_record["gate_verdict"]["review_sha256"]:
            raise HtError("cgate finalizer review hash/linkage changed before commit")
        if review["merge_record_ref"] != updated_record["id"]:
            raise HtError("cgate finalizer review/MR linkage is inconsistent")
        if escalated:
            assert rq_document is not None and rq_id is not None
            if (
                review["escalation_ref"] != rq_id
                or rq_document["payload_ref"] != f"tier1/gate-reviews/{review_id}.json"
            ):
                raise HtError("cgate finalizer escalation linkage is inconsistent")

    result = {
        "record_id": record_id,
        "gate_review_ref": review_id,
        "gate_review_sha256": review_sha256,
        "verdict": verdict,
        "ratification_ref": rq_id,
    }
    return Plan(
        role="cgate",
        message=f"ht cgate finalize: {record_id} -> {verdict}",
        writes=writes,
        semantic=semantic,
    ), result


__all__ = ["FINALIZATION_FORMAT", "build_finalization_plan"]
