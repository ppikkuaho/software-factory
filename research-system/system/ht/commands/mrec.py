"""Merge-record lifecycle commands: harness creation and cgate verdict fill."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
from typing import Any

from composition_gate.normalization import comparison_key
from composition_gate.screen import CHECK_NAMES
from composition_gate.classification import (
    ScreenValidationError,
    validate_engine_output,
)

from .. import jsonio
from ..errors import HtError, HtUsageError
from ..paths import Root
from ..pipeline import DocWrite, Plan
from ..references import canonical_json_sha256, resolve_ref
from ._common import Ctx, current_global_epoch, today


_CANDIDATE_REF = re.compile(
    r"tree#(?P<lane>[^/#\s]+)/node#(?P<node>[^/#\s]+)"
)
_RECORD_ID = re.compile(r"MR-(?P<ordinal>[0-9]+)")
_SCREEN_RESULTS = frozenset({"pass", "fail", "n/a"})
_SCREEN_RESULT_FIELDS = frozenset({"check", "result", "detail", "inputs"})
_LIST_HEADER = "id  candidate_ref  lane  verdict  created  consumed_epoch"
_LIST_PARTITIONS = (
    "awaiting-verdict",
    "land-ready",
    "verdict-issued/unconsumed",
    "consumed",
)


def _canonical_candidate_ref(candidate_ref: str) -> str:
    if not isinstance(candidate_ref, str):
        raise HtUsageError(
            "merge-record candidate ref must be tree#<lane>/node#<id> "
            "(item 1 W6/Q5)"
        )
    normalized = candidate_ref.strip()
    match = _CANDIDATE_REF.fullmatch(normalized)
    if match is None:
        raise HtUsageError(
            "merge-record candidate ref must be canonical "
            "tree#<lane>/node#<id>; bare node refs are not accepted "
            "(item 1 W6/Q5)"
        )
    return f"tree#{match.group('lane')}/node#{match.group('node')}"


def _normalized_string_list(values: list[str], label: str) -> list[str]:
    if not isinstance(values, list):
        raise HtUsageError(f"merge-record scope {label} must be a repeatable string list")
    normalized: list[str] = []
    seen: set[str] = set()
    for ordinal, value in enumerate(values, start=1):
        if not isinstance(value, str) or not value.strip():
            raise HtUsageError(
                f"merge-record scope {label} value {ordinal} must be a non-empty string"
            )
        item = value.strip()
        key = comparison_key(item)
        if key not in seen:
            seen.add(key)
            normalized.append(item)
    return normalized


def _normalized_scope_path_list(values: list[str], label: str) -> list[str]:
    normalized = _normalized_string_list(values, label)
    for value in normalized:
        if (
            Path(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
            or ".." in value.split("/")
            or "\\" in value
        ):
            raise HtUsageError(
                f"merge-record scope {label} value {value!r} must be relative and "
                "may not contain '..' path segments or backslashes"
            )
    return normalized


def _normalized_scope(
    candidate_ref: str,
    lane: str,
    seats: list[str],
    surfaces: list[str],
    globs: list[str],
) -> dict:
    if not isinstance(lane, str) or not lane.strip():
        raise HtUsageError("merge-record creation requires a non-empty scope lane (D1)")
    normalized_lane = lane.strip()
    candidate_match = _CANDIDATE_REF.fullmatch(candidate_ref)
    assert candidate_match is not None
    candidate_lane = candidate_match.group("lane")
    if normalized_lane != candidate_lane:
        raise HtUsageError(
            f"merge-record scope lane '{normalized_lane}' must equal candidate_ref lane "
            f"'{candidate_lane}' (D1 mechanical consistency)"
        )
    return {
        "lane": normalized_lane,
        "seats": _normalized_string_list(seats, "seat"),
        "surfaces": _normalized_scope_path_list(surfaces, "surface"),
        "globs": _normalized_scope_path_list(globs, "glob"),
    }


def _normalized_screen_results(screen_results: list[dict]) -> list[dict]:
    if not isinstance(screen_results, list) or not screen_results:
        raise HtUsageError(
            "merge-record screen requires at least one result (item 1 W6)"
        )

    normalized: list[dict] = []
    for ordinal, result in enumerate(screen_results, start=1):
        if not isinstance(result, dict):
            raise HtUsageError(
                f"merge-record screen result {ordinal} must be an object "
                "(item 1 W6)"
            )
        fields = set(result)
        if not {"check", "result"} <= fields or not fields <= _SCREEN_RESULT_FIELDS:
            raise HtUsageError(
                f"merge-record screen result {ordinal} requires check/result and "
                "permits only optional detail/inputs (item 7 W2)"
            )

        check = result["check"]
        if not isinstance(check, str) or not check.strip():
            raise HtUsageError(
                f"merge-record screen result {ordinal} requires a non-empty check "
                "(item 1 W6)"
            )
        verdict = result["result"]
        if not isinstance(verdict, str) or verdict not in _SCREEN_RESULTS:
            raise HtUsageError(
                f"merge-record screen result {ordinal} has unknown result "
                f"'{verdict}' (pass|fail|n/a) (item 1 W6)"
            )
        if "detail" in result and not isinstance(result["detail"], str):
            raise HtUsageError(
                f"merge-record screen result {ordinal} detail must be a string "
                "(item 7 W2)"
            )
        if "inputs" in result and not isinstance(result["inputs"], dict):
            raise HtUsageError(
                f"merge-record screen result {ordinal} inputs must be an object "
                "(item 7 W2; D10 rail 1)"
            )

        item: dict[str, Any] = {"check": check.strip(), "result": verdict}
        if "detail" in result:
            item["detail"] = result["detail"]
        if "inputs" in result:
            item["inputs"] = copy.deepcopy(result["inputs"])
        normalized.append(item)
    return normalized


def _normalized_engine_screen_results(screen_results: list[dict]) -> list[dict]:
    normalized = _normalized_screen_results(screen_results)
    checks = tuple(item["check"] for item in normalized)
    if checks != CHECK_NAMES:
        raise HtUsageError(
            "computed screen must contain exactly the five canonical checks in engine "
            f"order: {', '.join(CHECK_NAMES)}"
        )
    for ordinal, item in enumerate(normalized, start=1):
        if "detail" not in item or not item["detail"].strip():
            raise HtUsageError(
                f"computed screen result {ordinal} ({item['check']}) requires "
                "non-empty detail"
            )
        inputs = item.get("inputs")
        if not isinstance(inputs, dict) or not inputs:
            raise HtUsageError(
                f"computed screen result {ordinal} ({item['check']}) requires a "
                "non-empty inputs object (D10 rail 1)"
            )
    return normalized


def _resolve_var_ref(root: Path, value: str, label: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip():
        raise HtUsageError(f"merge-record screen {label} must be a non-empty ref")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    root_resolved = root.resolve()
    var_resolved = (root / "var").resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(var_resolved):
        raise HtUsageError(
            f"merge-record screen {label} must resolve below the research root's var/"
        )
    try:
        relative = resolved.relative_to(root_resolved).as_posix()
    except ValueError as exc:
        raise HtUsageError(
            f"merge-record screen {label} must resolve below the research root's var/"
        ) from exc
    if not relative.startswith("var/"):
        raise HtUsageError(
            f"merge-record screen {label} must resolve below the research root's var/"
        )
    return relative, resolved


def _current_physical_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise HtUsageError(f"cannot inspect physical HEAD before screen transcription: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise HtUsageError(f"cannot inspect physical HEAD before screen transcription: {detail}")
    return result.stdout.strip()


def _file_fingerprint(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _read_stable_bytes(path: Path, label: str) -> tuple[bytes, tuple[int, int, int, int, int]]:
    try:
        with path.open("rb") as stream:
            before = _file_fingerprint(os.fstat(stream.fileno()))
            content = stream.read()
            after = _file_fingerprint(os.fstat(stream.fileno()))
    except OSError as exc:
        raise HtUsageError(f"cannot read merge-record screen {label} from '{path}': {exc}") from exc
    if before != after or len(content) != after[2]:
        raise HtError(f"merge-record screen {label} changed while it was being read; retry")
    return content, after


def _birth_screen(screen_results: list[dict], screen_log_ref: str | None) -> dict:
    """Create the extended birth group; evidence identity is null until screen."""
    del screen_log_ref
    return {
        "results": screen_results,
        "output_ref": None,
        "log_ref": None,
        "log_sha256": None,
        "output_sha256": None,
        "computed": None,
        "head_commit": None,
        "head_tree": None,
        "config_hash": None,
        "engine_version": None,
    }


def _list_row(entry) -> str:
    record = entry.as_dict()
    gate_verdict = record["gate_verdict"]
    verdict = (
        gate_verdict.get("verdict")
        if isinstance(gate_verdict, dict)
        else None
    )
    consumed_epoch = record["consumed_epoch"]
    return "  ".join(
        (
            record["id"],
            record["candidate_ref"],
            record["scope"]["lane"],
            verdict if verdict is not None else "—",
            record["created"],
            str(consumed_epoch) if consumed_epoch is not None else "—",
        )
    )


def _render_list(selected, status: str) -> str:
    if status != "all":
        lines = [_LIST_HEADER]
        lines.extend(_list_row(entry) for entry in selected)
        return "\n".join(lines) + "\n"

    grouped = {partition: [] for partition in _LIST_PARTITIONS}
    for entry in selected:
        grouped[entry.partition].append(entry)

    lines: list[str] = []
    for partition in _LIST_PARTITIONS:
        lines.extend((partition, _LIST_HEADER))
        lines.extend(_list_row(entry) for entry in grouped[partition])
    return "\n".join(lines) + "\n"


def list_records(
    root: Root,
    *,
    status: str = "all",
    last: int | None = None,
    as_json: bool = False,
) -> int:
    """Print the read-only committed merge-record view."""
    # Keep the committed-view dependency behind the read-only command boundary.
    # In particular, importing the ordinary mutation CLI must not require the
    # independently packaged composition-gate discovery implementation.
    from .. import mrec_views

    snapshot = mrec_views.load_merge_record_snapshot(root.path)
    selected = snapshot.select(status=status, last=last)

    if as_json:
        output = jsonio.dumps([entry.as_dict() for entry in selected])
    else:
        output = _render_list(selected, status)
    sys.stdout.write(output)
    return 0


def _next_record_id(ctx: Ctx) -> str:
    """Allocate within the global Tier-1 merge-record namespace.

    The caller holds the item-1 global mutex across this scan and commit; that
    critical section prevents concurrent mrec creates from minting the same ID.
    """
    records_dir = ctx.root.tier1_dir / "merge-records"
    ordinals: list[int] = []
    if records_dir.is_dir():
        for path in records_dir.glob("MR-*.json"):
            match = _RECORD_ID.fullmatch(path.stem)
            if match is not None:
                ordinals.append(int(match.group("ordinal")))
    return f"MR-{max(ordinals, default=0) + 1}"


def create(
    ctx: Ctx,
    candidate_ref: str,
    lane_verdict: str,
    lane_adjudication_ref: str,
    scope_lane: str,
    scope_seats: list[str],
    scope_surfaces: list[str],
    scope_globs: list[str],
    screen_results: list[dict],
    screen_log_ref: str | None,
) -> Plan:
    """Build a normalized harness-authored merge-record creation plan."""
    candidate_resolved = resolve_ref(ctx.root, candidate_ref, expected={"node"})
    candidate = candidate_resolved.canonical
    if not isinstance(lane_verdict, str) or not lane_verdict.strip():
        raise HtUsageError(
            "merge-record creation requires a non-empty lane verdict "
            "(item 1 W6)"
        )
    if screen_log_ref is not None and not isinstance(screen_log_ref, str):
        raise HtUsageError(
            "merge-record screen log ref must be a string or null (item 1 W6)"
        )
    if not screen_results and screen_log_ref is not None:
        raise HtUsageError(
            "merge-record screen log ref requires supplied screen results at creation"
        )

    lane_adjudication = resolve_ref(
        ctx.root,
        lane_adjudication_ref,
        expected={"adjudication"},
    )
    if lane_adjudication.tree != candidate_resolved.tree:
        raise HtError(
            f"lane adjudication {lane_adjudication.canonical} is not in candidate "
            f"tree {candidate_resolved.tree}"
        )
    header = lane_adjudication.document
    if header.get("node_ref") != candidate:
        raise HtError(
            f"lane adjudication {lane_adjudication.canonical} belongs to "
            f"{header.get('node_ref')!r}, not candidate {candidate}"
        )
    if header.get("verdict") not in {"granted", "demoted"}:
        raise HtError(
            f"lane adjudication {lane_adjudication.canonical} verdict is "
            f"{header.get('verdict')!r}; requires granted or demoted"
        )

    backing_claims = snapshot_backing_claims(ctx.root, candidate)
    backing_refs = {item["ref"] for item in backing_claims}
    if header.get("claim_ref") not in backing_refs:
        raise HtError(
            f"lane adjudication {lane_adjudication.canonical} claim "
            f"{header.get('claim_ref')!r} is not a frozen backing claim"
        )
    issue_refs = set(candidate_resolved.issue_refs) | set(lane_adjudication.issue_refs)
    for item in backing_claims:
        issue_refs.update(resolve_ref(ctx.root, item["ref"], expected={"claim"}).issue_refs)
    if len(issue_refs) > 1:
        raise HtError(
            "merge-record candidate, lane adjudication, and backing claims have "
            f"conflicting issue affiliations: {', '.join(sorted(issue_refs))}"
        )

    record_id = _next_record_id(ctx)
    record = {
        "id": record_id,
        "candidate_ref": candidate,
        "lane_verdict": lane_verdict.strip(),
        "lane_adjudication_ref": lane_adjudication.canonical,
        "backing_claims": backing_claims,
        "scope": _normalized_scope(
            candidate,
            scope_lane,
            scope_seats,
            scope_surfaces,
            scope_globs,
        ),
        "screen": _birth_screen(
            _normalized_screen_results(screen_results) if screen_results else [],
            screen_log_ref,
        ),
        "gate_verdict": None,
        "watch_link": None,
        "created": today(),
        "consumed_epoch": None,
    }
    return Plan(
        role=ctx.role,
        message=f"ht mrec create: {record_id} ({candidate})",
        writes=[
            DocWrite(
                ctx.root.merge_record_json(record_id),
                "merge_record",
                None,
                record,
            )
        ],
    )


def _claim_adjudication_ref(root: Root, claim_ref: str) -> str:
    claim = resolve_ref(root, claim_ref, expected={"claim"})
    assert claim.tree is not None
    dispatch = (claim.metadata or {}).get("dispatch")
    if not isinstance(dispatch, dict):
        raise HtError(f"claim {claim_ref} has no resolvable source dispatch")
    matches: list[str] = []
    for adjudication_ref in dispatch.get("adjudications", []):
        if not isinstance(adjudication_ref, str):
            continue
        try:
            adjudication = resolve_ref(
                root, adjudication_ref, expected={"adjudication"}
            )
        except (HtError, HtUsageError):
            continue
        header = adjudication.document
        if (
            header.get("claim_ref") == claim.canonical
            and header.get("verdict") in {"granted", "demoted"}
        ):
            matches.append(adjudication.canonical)
    if len(matches) != 1:
        raise HtError(
            f"claim {claim.canonical} requires exactly one canonical grant/demotion "
            f"adjudication; found {len(matches)}"
        )
    return matches[0]


def _valid_revalidation(claim: dict, global_epoch: int) -> bool:
    value = claim.get("revalidation")
    claim_epoch = claim.get("epoch")
    return (
        isinstance(value, dict)
        and isinstance(value.get("date"), str)
        and bool(value["date"])
        and isinstance(value.get("epoch"), int)
        and not isinstance(value.get("epoch"), bool)
        and isinstance(claim_epoch, int)
        and not isinstance(claim_epoch, bool)
        and 0 <= claim_epoch <= value["epoch"] <= global_epoch
        and isinstance(value.get("ref"), str)
        and bool(value["ref"])
    )


def _claim_merge_eligible(claim: dict, global_epoch: int) -> bool:
    return claim.get("status") == "granted" and (
        claim.get("standing_class") == "trunk"
        or _valid_revalidation(claim, global_epoch)
    )


def snapshot_backing_claims(root: Root, candidate_ref: str) -> list[dict]:
    candidate = resolve_ref(root, candidate_ref, expected={"node"})
    node = candidate.document
    granted = [
        claim
        for claim in node.get("claims", [])
        if isinstance(claim, dict) and claim.get("status") == "granted"
    ]
    if not granted:
        raise HtError(f"candidate {candidate.canonical} has no granted backing claims")
    global_epoch = current_global_epoch(Ctx(root, "harness"))
    ineligible = [
        claim.get("id")
        for claim in granted
        if not _claim_merge_eligible(claim, global_epoch)
    ]
    if ineligible:
        raise HtError(
            f"candidate {candidate.canonical} has merge-ineligible granted claims: "
            f"{', '.join(str(value) for value in ineligible)}"
        )
    snapshots: list[dict] = []
    assert candidate.tree is not None
    for claim in granted:
        claim_ref = f"tree#{candidate.tree}/claim#{claim['id']}"
        snapshots.append(
            {
                "ref": claim_ref,
                "sha256": canonical_json_sha256(claim),
                "adjudication_ref": _claim_adjudication_ref(root, claim_ref),
            }
        )
    return sorted(snapshots, key=lambda item: item["ref"])


def verify_backing_snapshot(root: Root, record: dict) -> list[dict]:
    candidate = record.get("candidate_ref")
    if not isinstance(candidate, str):
        raise HtError("merge record has no canonical candidate_ref")
    expected = record.get("backing_claims")
    if not isinstance(expected, list):
        raise HtError("legacy-unresolved: merge record has no backing_claims anchor")
    current = snapshot_backing_claims(root, candidate)
    if current != expected:
        raise HtError(
            "merge-record backing claim identities/hashes/adjudications drifted; "
            "create and review a new merge record"
        )
    return current


def provenance(root: Root, record_id: str, *, as_json: bool) -> int:
    if _RECORD_ID.fullmatch(record_id) is None:
        raise HtUsageError("merge-record id must have the form MR-<n>")
    resolved_record = resolve_ref(
        root, f"merge-record#{record_id}", expected={"merge-record"}
    )
    record = resolved_record.document
    merge_record_ref = f"merge-record#{record_id}"
    has_lane = "lane_adjudication_ref" in record
    has_backing = "backing_claims" in record
    if not has_lane and not has_backing:
        result = {
            "status": "legacy-unresolved",
            "merge_record_ref": merge_record_ref,
            "reason": "record predates Wave-A provenance anchors",
        }
        sys.stdout.write(jsonio.dumps(result))
        return 0
    if has_lane != has_backing:
        raise HtError(
            "merge record has a broken partial provenance anchor; "
            "lane_adjudication_ref and backing_claims must appear together"
        )

    backing = verify_backing_snapshot(root, record)
    candidate = resolve_ref(root, record["candidate_ref"], expected={"node"})
    lane = resolve_ref(
        root, record["lane_adjudication_ref"], expected={"adjudication"}
    )
    if lane.document.get("node_ref") != candidate.canonical:
        raise HtError("merge-record lane adjudication does not belong to the candidate")
    if lane.document.get("verdict") not in {"granted", "demoted"}:
        raise HtError("merge-record lane adjudication is not a grant/demotion")
    backing_refs = {item["ref"] for item in backing}
    if lane.document.get("claim_ref") not in backing_refs:
        raise HtError("merge-record lane adjudication does not name a backing claim")

    dispatch_refs: set[str] = set()
    report_rows: dict[str, dict] = {}
    adjudication_refs: set[str] = {lane.canonical}
    issue_refs: set[str] = set(candidate.issue_refs) | set(lane.issue_refs)
    backing_rows: list[dict] = []
    for item in backing:
        claim = resolve_ref(root, item["ref"], expected={"claim"})
        if canonical_json_sha256(claim.document) != item["sha256"]:
            raise HtError(f"merge-record claim hash mismatch for {claim.canonical}")
        adjudication = resolve_ref(
            root, item["adjudication_ref"], expected={"adjudication"}
        )
        if adjudication.document.get("claim_ref") != claim.canonical:
            raise HtError(
                f"merge-record adjudication {adjudication.canonical} does not name "
                f"claim {claim.canonical}"
            )
        dispatch = (claim.metadata or {}).get("dispatch")
        dispatch_ref = f"tree#{claim.tree}/dispatch#{dispatch['id']}"
        report_ref = f"tree#{claim.tree}/report#{dispatch['id']}"
        report = resolve_ref(root, report_ref, expected={"report"})
        dispatch_refs.add(dispatch_ref)
        report_rows[report_ref] = {
            "ref": report_ref,
            "sha256": (report.metadata or {})["sha256"],
        }
        adjudication_refs.add(adjudication.canonical)
        issue_refs.update(claim.issue_refs)
        issue_refs.update(adjudication.issue_refs)
        backing_rows.append(dict(item))
    if len(issue_refs) > 1:
        raise HtError(
            "merge-record provenance has conflicting issue affiliations: "
            + ", ".join(sorted(issue_refs))
        )
    result = {
        "status": "resolved",
        "merge_record_ref": merge_record_ref,
        "candidate_ref": candidate.canonical,
        "lane_adjudication_ref": lane.canonical,
        "backing_claims": sorted(backing_rows, key=lambda item: item["ref"]),
        "dispatch_refs": sorted(dispatch_refs),
        "report_refs": [report_rows[key] for key in sorted(report_rows)],
        "adjudication_refs": sorted(adjudication_refs),
        "issue_ref": next(iter(issue_refs), None),
    }
    del as_json  # JSON is the only fail-closed public representation in v1.
    sys.stdout.write(jsonio.dumps(result))
    return 0


def screen(
    ctx: Ctx,
    record_id: str,
    results_json: str,
    log_ref: str,
) -> Plan:
    """Overwrite a pre-verdict merge record with one computed screen result."""
    if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
        raise HtUsageError("merge-record id must have the form MR-<n> (item 7 W2)")
    if not isinstance(results_json, str) or not results_json.strip():
        raise HtUsageError("ht mrec screen requires a results JSON path (item 7 W2)")
    if not isinstance(log_ref, str) or not log_ref.strip():
        raise HtUsageError("ht mrec screen requires a non-empty log ref (item 7 W2)")

    path = ctx.root.merge_record_json(record_id)
    if not path.exists():
        raise HtUsageError(f"no such merge record '{record_id}'")
    old = jsonio.load(path)
    if old.get("consumed_epoch") is not None:
        raise HtError(
            f"cannot screen consumed merge record {record_id} "
            f"(consumed_epoch={old['consumed_epoch']}; D2)"
        )
    if old.get("gate_verdict") is not None:
        raise HtError(
            f"merge record {record_id} already has a gate verdict; screen is frozen "
            "after verdict under [R-i7-1] D2"
        )

    output_ref, output_path = _resolve_var_ref(
        ctx.root.path, results_json, "output ref"
    )
    log_ref_normalized, log_path = _resolve_var_ref(
        ctx.root.path, log_ref, "log ref"
    )
    head_before = _current_physical_head(ctx.root.path)
    evidence: dict[Path, tuple[bytes, tuple[int, int, int, int, int]]] = {}
    evidence[output_path] = _read_stable_bytes(output_path, "output")
    output_bytes = evidence[output_path][0]
    try:
        output = json.loads(output_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HtUsageError(
            f"cannot read merge-record screen results from '{output_path}': {exc}"
        ) from exc
    if not isinstance(output, dict):
        raise HtUsageError("merge-record screen results JSON must be an object")
    if output.get("record_id") != record_id:
        raise HtUsageError(
            "merge-record screen results record_id must match the target record "
            f"('{output.get('record_id')}' != '{record_id}')"
        )
    # Preserve the W2 transcription seam's precise diagnostics before the
    # stricter identity envelope is checked.
    _normalized_engine_screen_results(output.get("results"))
    try:
        results = validate_engine_output(output, record_id)
    except ScreenValidationError as exc:
        raise HtUsageError(f"invalid W2 screen output: {exc}") from exc

    if log_path not in evidence:
        evidence[log_path] = _read_stable_bytes(log_path, "log")
    log_bytes = evidence[log_path][0]

    head_commit = output["head_commit"]
    if head_commit is not None and head_before != head_commit:
        raise HtError(
            "merge-record screen output was computed at a different physical HEAD "
            f"({head_commit} != {head_before}); rerun ht-cgate screen before transcription"
        )

    def evidence_unchanged() -> None:
        if _current_physical_head(ctx.root.path) != head_before:
            raise HtError("physical HEAD moved during screen transcription; retry ht-cgate screen")
        for evidence_path, (_, expected) in evidence.items():
            try:
                actual = _file_fingerprint(evidence_path.stat())
            except OSError as exc:
                raise HtError(f"screen evidence changed before commit: {evidence_path}: {exc}") from exc
            if actual != expected:
                raise HtError(f"screen evidence changed before commit: {evidence_path}")

    new = dict(old)
    new["screen"] = {
        "results": copy.deepcopy(results),
        "output_ref": output_ref,
        "log_ref": log_ref_normalized,
        "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "computed": output["computed"],
        "head_commit": output["head_commit"],
        "head_tree": output["head_tree"],
        "config_hash": output["config_hash"],
        "engine_version": output["engine_version"],
    }
    return Plan(
        role=ctx.role,
        message=f"ht mrec screen: {record_id}",
        writes=[DocWrite(path, "merge_record", old, new)],
        semantic=evidence_unchanged,
    )


def verdict(
    ctx: Ctx,
    record_id: str,
    verdict: str,
    note: str | None,
) -> Plan:
    """Reject the retired MR-only verdict writer."""
    del ctx, record_id, verdict, note
    raise HtUsageError(
        "direct ht mrec verdict is disabled; use ht-cgate decide MR-N --root ROOT "
        "--execute so GR + MR (+ RQ) are finalized atomically ([R-i7-9]; D2)"
    )
