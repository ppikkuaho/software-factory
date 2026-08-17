"""Gate-hard deterministic plan-alignment coverage over one submitted package.

Q3 deliberately stops at machine facts: exact trace records, graph reachability,
and the package sidecar's explicit failure-path classification.  It does not
judge whether a design or criterion means the right thing; the Q4 semantic cell
owns that later evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from harnessd import notary, traceability


_REQUIREMENT_ID = re.compile(r"^R-\d+(?:\.\d+)*$")
_DR_ID = re.compile(r"^DR-\d+\w*$")
_DD_ID = re.compile(r"^DD-\d+$")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_LIVE_TAGS = {"decided", "delegated"}
_ALL_TAGS = _LIVE_TAGS | {"deferred"}


@dataclass(frozen=True)
class IntentRequirement:
    id: str
    tag: str
    must_never_fail: bool


@dataclass(frozen=True)
class CoverageEvaluation:
    ok: bool
    defects: tuple[str, ...]
    package: Path
    manifest: Optional[Path]
    report_path: Optional[Path]
    report_sha256: Optional[str]
    bundle_sha256: Optional[str]
    marker_sha256: str


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path.resolve()), str(root.resolve())]) == str(
            root.resolve()
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve_member(value, node_dir: Path, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty node-relative path")
    raw = Path(value)
    if raw.is_absolute():
        raise ValueError(f"{label} must be node-relative, got absolute path {value!r}")
    path = node_dir / raw
    if not _inside(path, node_dir):
        raise ValueError(f"{label} must stay inside the L2 node workspace: {value!r}")
    if not path.is_file():
        raise ValueError(f"{label} does not exist as a file: {value!r}")
    return path


def _strip_cell(value: str) -> str:
    text = value.strip()
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for marker in ("**", "__", "`"):
            if text.startswith(marker) and text.endswith(marker):
                text = text[len(marker):-len(marker)].strip()
                changed = True
    return text


def _split_markdown_row(raw: str) -> list[str]:
    body = raw.strip()
    if not (body.startswith("|") and body.endswith("|")):
        return []
    body = body[1:-1]
    cells = re.split(r"(?<!\\)\|", body)
    return [_strip_cell(cell.replace(r"\|", "|")) for cell in cells]


def _intent_requirements(receipt: dict) -> tuple[list[IntentRequirement], list[str], str]:
    if not isinstance(receipt, dict):
        return [], ["MISSING-INTENT-SPEC-RECEIPT"], ""
    fingerprint = str(receipt.get("fingerprint") or (receipt.get("stamp") or {}).get("sha256") or "")
    try:
        checked = notary.check(receipt)
    except (OSError, TypeError, ValueError) as exc:
        return [], [f"STALE-INTENT-SPEC-RECEIPT: {exc}"], fingerprint
    if not checked.ok:
        return [], ["STALE-INTENT-SPEC-RECEIPT"], fingerprint
    try:
        artifact = Path(str(receipt["artifact"]))
        lines = artifact.read_text(encoding="utf-8", errors="replace").splitlines()
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return [], [f"MALFORMED-INTENT-REQUIREMENTS: {exc}"], fingerprint

    header_index = None
    header: list[str] = []
    for index, raw in enumerate(lines):
        cells = _split_markdown_row(raw)
        normalized = {cell.casefold() for cell in cells}
        if {"id", "tag", "mnf"}.issubset(normalized):
            header_index = index
            header = [cell.casefold() for cell in cells]
            break
    if header_index is None:
        return (
            [],
            [
                "MALFORMED-INTENT-REQUIREMENTS: canonical Requirements table "
                "with ID, Tag, and MNF columns is absent"
            ],
            fingerprint,
        )
    if header_index + 1 >= len(lines):
        return [], ["MALFORMED-INTENT-REQUIREMENTS: table separator is absent"], fingerprint
    separator = _split_markdown_row(lines[header_index + 1])
    if len(separator) != len(header) or not all(
        _TABLE_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in separator
    ):
        return [], ["MALFORMED-INTENT-REQUIREMENTS: table separator is malformed"], fingerprint

    id_index = header.index("id")
    tag_index = header.index("tag")
    mnf_index = header.index("mnf")
    requirements: list[IntentRequirement] = []
    defects: list[str] = []
    seen: set[str] = set()
    for raw in lines[header_index + 2:]:
        cells = _split_markdown_row(raw)
        if not cells:
            break
        if len(cells) != len(header):
            defects.append(
                "MALFORMED-INTENT-REQUIREMENTS: requirements row has "
                f"{len(cells)} cells; expected {len(header)}"
            )
            continue
        requirement_id = cells[id_index]
        tag = cells[tag_index].casefold()
        mnf = cells[mnf_index]
        if not _REQUIREMENT_ID.fullmatch(requirement_id):
            defects.append(f"MALFORMED-INTENT-REQUIREMENTS: invalid ID {requirement_id!r}")
            continue
        if requirement_id in seen:
            defects.append(f"DUP-ID-{requirement_id}")
            continue
        seen.add(requirement_id)
        if tag not in _ALL_TAGS:
            defects.append(
                f"MALFORMED-INTENT-REQUIREMENTS: {requirement_id} has invalid tag {tag!r}"
            )
            continue
        if mnf.casefold() == "yes":
            must_never_fail = True
        elif mnf in {"—", "-"}:
            must_never_fail = False
        else:
            defects.append(
                f"MALFORMED-INTENT-REQUIREMENTS: {requirement_id} has invalid MNF {mnf!r}"
            )
            continue
        requirements.append(
            IntentRequirement(
                id=requirement_id,
                tag=tag,
                must_never_fail=must_never_fail,
            )
        )
    if not requirements:
        defects.append("MALFORMED-INTENT-REQUIREMENTS: no requirement rows found")
    return requirements, defects, fingerprint


def _load_manifest(
    payload: dict,
    node_dir: Path,
) -> tuple[Optional[Path], list[Path], dict[Path, tuple[str, ...]], list[dict], list[str]]:
    defects: list[str] = []
    try:
        manifest = _resolve_member(
            payload.get("coverage_manifest"),
            node_dir,
            label="coverage_manifest",
        )
    except ValueError as exc:
        return None, [], {}, [], [f"MALFORMED-PLAN-ALIGNMENT-MANIFEST: {exc}"]
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return manifest, [], {}, [], [f"MALFORMED-PLAN-ALIGNMENT-MANIFEST: {exc}"]
    if not isinstance(data, dict):
        return (
            manifest,
            [],
            {},
            [],
            ["MALFORMED-PLAN-ALIGNMENT-MANIFEST: sidecar must be a JSON object"],
        )
    expected_fields = {"schema_version", "artifacts", "failure_path_criteria"}
    if set(data) != expected_fields:
        defects.append(
            "MALFORMED-PLAN-ALIGNMENT-MANIFEST: sidecar fields must be exactly "
            f"{sorted(expected_fields)}, got {sorted(data)}"
        )
    if data.get("schema_version") != 1:
        defects.append(
            "MALFORMED-PLAN-ALIGNMENT-MANIFEST: schema_version must equal 1"
        )

    artifacts: list[Path] = []
    artifact_trace_ids: dict[Path, tuple[str, ...]] = {}
    artifact_rows = data.get("artifacts")
    if not isinstance(artifact_rows, list) or not artifact_rows:
        defects.append(
            "MALFORMED-PLAN-ALIGNMENT-MANIFEST: artifacts must be a non-empty list"
        )
    else:
        seen_paths: set[Path] = set()
        seen_trace_ids: set[str] = set()
        for index, value in enumerate(artifact_rows):
            if not isinstance(value, dict) or set(value) != {"path", "trace_ids"}:
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: "
                    f"artifacts[{index}] must contain exactly path and trace_ids"
                )
                continue
            trace_ids = value.get("trace_ids")
            if (
                not isinstance(trace_ids, list)
                or not trace_ids
                or not all(isinstance(item, str) and item.strip() for item in trace_ids)
            ):
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: "
                    f"artifacts[{index}].trace_ids must be a non-empty string list"
                )
                continue
            normalized_trace_ids = tuple(item.strip() for item in trace_ids)
            if len(set(normalized_trace_ids)) != len(normalized_trace_ids):
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: duplicate trace id inside "
                    f"artifacts[{index}]"
                )
                continue
            duplicate_refs = set(normalized_trace_ids) & seen_trace_ids
            if duplicate_refs:
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: trace ids appear in multiple "
                    f"artifact rows: {sorted(duplicate_refs)}"
                )
                continue
            try:
                artifact = _resolve_member(
                    value.get("path"),
                    node_dir,
                    label=f"artifacts[{index}]",
                )
            except ValueError as exc:
                defects.append(f"MALFORMED-PLAN-ALIGNMENT-MANIFEST: {exc}")
                continue
            if artifact.suffix.casefold() != ".md":
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: "
                    f"artifacts[{index}] must name a Markdown file"
                )
                continue
            resolved = artifact.resolve()
            if resolved in seen_paths:
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: duplicate artifact "
                    f"{value.get('path')!r}"
                )
                continue
            seen_paths.add(resolved)
            seen_trace_ids.update(normalized_trace_ids)
            artifacts.append(artifact)
            artifact_trace_ids[resolved] = normalized_trace_ids

    mappings = data.get("failure_path_criteria")
    normalized_mappings: list[dict] = []
    if not isinstance(mappings, list):
        defects.append(
            "MALFORMED-PLAN-ALIGNMENT-MANIFEST: "
            "failure_path_criteria must be a list"
        )
    else:
        for index, row in enumerate(mappings):
            if not isinstance(row, dict) or set(row) != {"requirement_id", "test_id"}:
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: "
                    f"failure_path_criteria[{index}] must contain exactly "
                    "requirement_id and test_id"
                )
                continue
            requirement_id = row.get("requirement_id")
            test_id = row.get("test_id")
            if not all(isinstance(item, str) and item.strip() for item in (requirement_id, test_id)):
                defects.append(
                    "MALFORMED-PLAN-ALIGNMENT-MANIFEST: "
                    f"failure_path_criteria[{index}] values must be non-empty strings"
                )
                continue
            normalized_mappings.append(
                {
                    "requirement_id": requirement_id.strip(),
                    "test_id": test_id.strip(),
                }
            )
    return manifest, artifacts, artifact_trace_ids, normalized_mappings, defects


def _bundle_sha(
    *,
    marker: Path,
    package: Path,
    manifest: Optional[Path],
    artifacts: list[Path],
    node_dir: Path,
    intent_fingerprint: str,
) -> str:
    members: dict[str, dict] = {}
    for path in [marker, package, *([manifest] if manifest is not None else []), *artifacts]:
        if path is None:
            continue
        try:
            key = path.resolve().relative_to(node_dir.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            key = str(path)
        members[key] = notary.stamp(path)
    canonical = json.dumps(
        {
            "intent_fingerprint": intent_fingerprint,
            "members": members,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _collect_elements(paths: list[Path]) -> tuple[list[traceability.TraceElement], list[str]]:
    records: list[traceability.TraceElement] = []
    defects: list[str] = []
    for path in paths:
        parsed, errors = traceability.parse_artifact(path)
        records.extend(parsed)
        for error in errors:
            defects.append(f"MALFORMED-TRACE-{path.name}:{error}")

    seen: dict[str, traceability.TraceElement] = {}
    for record in records:
        missing = sorted(traceability.TRACE_FIELDS - set(record.fields))
        if not record.level:
            missing.append("level")
        if not record.node:
            missing.append("node")
        if missing:
            defects.append(f"MALFORMED-TRACE-{record.id}")
        if record.kind not in traceability.TRACE_KINDS:
            defects.append(f"MALFORMED-TRACE-{record.id}")
        previous = seen.get(record.id)
        if previous is not None:
            defects.append(f"DUP-ID-{record.id}")
        else:
            seen[record.id] = record
        if record.kind in {"decision", "adr"} and not _DD_ID.fullmatch(record.id):
            defects.append(f"MALFORMED-TRACE-{record.id}")
        if record.kind == "derived" and not _DR_ID.fullmatch(record.id):
            defects.append(f"MALFORMED-TRACE-{record.id}")
    return records, defects


def _write_report(node_dir: Path, bundle_sha256: str, report: dict) -> tuple[Path, str]:
    path = node_dir / "plan" / f"plan-alignment-coverage.{bundle_sha256}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                stamped = notary.stamp(path, read_only=True)
                return path, str(stamped["sha256"])
            os.chmod(path, 0o644)
        except OSError:
            pass
    path.write_text(content, encoding="utf-8")
    stamped = notary.stamp(path, read_only=True)
    return path, str(stamped["sha256"])


def evaluate_submission(
    *,
    node_address: str,
    node_dir: Path,
    marker: Path,
    marker_payload: dict,
    package: Path,
    binding: dict,
) -> CoverageEvaluation:
    """Evaluate and report the Q3 floor for one exact readiness bundle."""

    marker_sha = str(notary.stamp(marker).get("sha256") or "")
    requirements, defects, intent_fingerprint = _intent_requirements(
        binding.get("intent_spec_receipt")
    )
    manifest, artifacts, artifact_trace_ids, mappings, manifest_defects = _load_manifest(
        marker_payload,
        node_dir,
    )
    defects.extend(manifest_defects)
    scan_paths: list[Path] = [package]
    for artifact in artifacts:
        if artifact.resolve() != package.resolve():
            scan_paths.append(artifact)
    bundle_sha = _bundle_sha(
        marker=marker,
        package=package,
        manifest=manifest,
        artifacts=artifacts,
        node_dir=node_dir,
        intent_fingerprint=intent_fingerprint,
    )
    records, trace_defects = _collect_elements(scan_paths)
    defects.extend(trace_defects)

    records_by_artifact: dict[Path, set[str]] = {}
    for record in records:
        records_by_artifact.setdefault(record.artifact.resolve(), set()).add(record.id)
    for artifact, declared_ids in artifact_trace_ids.items():
        actual_ids = records_by_artifact.get(artifact, set())
        for declared_id in declared_ids:
            if declared_id not in actual_ids:
                defects.append(f"SIDECAR-DANGLING-{declared_id}")

    requirement_by_id = {item.id: item for item in requirements}
    record_by_id: dict[str, traceability.TraceElement] = {}
    for record in records:
        if record.id not in record_by_id:
            record_by_id[record.id] = record
        if record.id in requirement_by_id:
            defects.append(f"DUP-ID-{record.id}")

    live_ids = {
        requirement.id
        for requirement in requirements
        if requirement.tag in _LIVE_TAGS
    }
    deferred_ids = {
        requirement.id
        for requirement in requirements
        if requirement.tag == "deferred"
    }

    valid_dr: set[str] = set()
    for record in records:
        if record.kind != "derived":
            continue
        if record.serves and all(target in live_ids for target in record.serves):
            valid_dr.add(record.id)
        else:
            defects.append(f"DR-UNSERVED-{record.id}")

    edges: dict[str, set[str]] = {}
    for requirement in requirements:
        if "." in requirement.id:
            edges.setdefault(requirement.id, set()).add(
                requirement.id.rsplit(".", 1)[0]
            )
    for record in records:
        if record.kind in {"decision", "adr"}:
            continue
        linked = set(record.serves)
        if _REQUIREMENT_ID.fullmatch(record.id) and "." in record.id:
            parent = record.id.rsplit(".", 1)[0]
            linked.add(parent)
            if parent not in requirement_by_id and parent not in record_by_id:
                defects.append(f"DANGLING-PARENT-{record.id}")
            if record.serves and parent not in record.serves:
                defects.append(f"TRACE-CONTRADICTION-{record.id}")
        edges.setdefault(record.id, set()).update(linked)
        for target in record.serves:
            if target not in requirement_by_id and target not in record_by_id:
                defects.append(f"DANGLE-{target}")

    root_cache: dict[str, frozenset[str]] = {}

    def roots(element_id: str, visiting: Optional[set[str]] = None) -> frozenset[str]:
        if element_id in root_cache:
            return root_cache[element_id]
        if element_id in live_ids:
            return frozenset({element_id})
        if element_id in deferred_ids:
            return frozenset()
        record = record_by_id.get(element_id)
        if record is not None and record.kind == "derived" and element_id not in valid_dr:
            return frozenset()
        active = set(visiting or ())
        if element_id in active:
            return frozenset()
        active.add(element_id)
        found: set[str] = set()
        for target in edges.get(element_id, ()):
            found.update(roots(target, active))
        result = frozenset(found)
        root_cache[element_id] = result
        return result

    def reaches(element_id: str, requirement_id: str) -> bool:
        if element_id == requirement_id:
            return True
        seen: set[str] = set()
        stack = [element_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for target in edges.get(current, ()):
                if target == requirement_id:
                    return True
                target_record = record_by_id.get(target)
                if (
                    target_record is not None
                    and target_record.kind == "derived"
                    and target not in valid_dr
                ):
                    continue
                stack.append(target)
        return False

    for record in records:
        if record.kind in {"requirement", "design", "test"} and not roots(record.id):
            defects.append(f"ORPHAN-{record.id}")

    valid_failure_tests: set[tuple[str, str]] = set()
    for row in mappings:
        requirement_id = row["requirement_id"]
        test_id = row["test_id"]
        requirement = requirement_by_id.get(requirement_id)
        if requirement is None:
            defects.append(f"SIDECAR-DANGLING-{requirement_id}")
            continue
        test = record_by_id.get(test_id)
        if (
            test is None
            or test.kind != "test"
            or not requirement.must_never_fail
            or not reaches(test_id, requirement_id)
        ):
            defects.append(f"SIDECAR-DANGLING-{test_id}")
            continue
        valid_failure_tests.add((requirement_id, test_id))

    requirement_rows: list[dict] = []
    for requirement in sorted(requirements, key=lambda item: item.id):
        if requirement.tag == "deferred":
            requirement_rows.append(
                {
                    "id": requirement.id,
                    "tag": requirement.tag,
                    "must_never_fail": requirement.must_never_fail,
                    "design_ids": [],
                    "test_ids": [],
                    "failure_path_test_ids": [],
                    "disposition": "exempt",
                }
            )
            continue
        design_ids = sorted(
            record.id
            for record in records
            if record.kind == "design" and reaches(record.id, requirement.id)
        )
        test_ids = sorted(
            record.id
            for record in records
            if record.kind == "test" and reaches(record.id, requirement.id)
        )
        failure_ids = sorted(
            test_id
            for mapped_requirement, test_id in valid_failure_tests
            if reaches(test_id, requirement.id)
            and mapped_requirement == requirement.id
        )
        row_defects: list[str] = []
        if not design_ids:
            row_defects.append(f"UNCOVERED-{requirement.id}")
        if not test_ids:
            row_defects.append(f"UNTESTED-{requirement.id}")
        if requirement.must_never_fail and not failure_ids:
            row_defects.append(f"MNF-NO-FAILURE-PATH-{requirement.id}")
        defects.extend(row_defects)
        requirement_rows.append(
            {
                "id": requirement.id,
                "tag": requirement.tag,
                "must_never_fail": requirement.must_never_fail,
                "design_ids": design_ids,
                "test_ids": test_ids,
                "failure_path_test_ids": failure_ids,
                "disposition": "pass" if not row_defects else "refused",
            }
        )

    # Stable first-seen ordering keeps repair payloads deterministic without
    # multiplying the same graph defect through multiple validation branches.
    unique_defects: list[str] = []
    seen_defects: set[str] = set()
    for defect in defects:
        if defect not in seen_defects:
            seen_defects.add(defect)
            unique_defects.append(defect)

    report = {
        "schema_version": 1,
        "status": "pass" if not unique_defects else "refused",
        "node_address": node_address,
        "package": str(package),
        "coverage_manifest": str(manifest) if manifest is not None else None,
        "bundle_sha256": bundle_sha,
        "intent_fingerprint": intent_fingerprint,
        "artifacts": [
            {
                "path": str(path),
                "sha256": notary.stamp(path).get("sha256"),
            }
            for path in scan_paths
        ],
        "requirements": requirement_rows,
        "deferred_requirements": [
            {"id": requirement.id, "exempt_reason": "deferred"}
            for requirement in sorted(requirements, key=lambda item: item.id)
            if requirement.tag == "deferred"
        ],
        "excluded_decisions": sorted(
            record.id for record in records if record.kind in {"decision", "adr"}
        ),
        "defects": unique_defects,
    }
    try:
        report_path, report_sha = _write_report(node_dir, bundle_sha, report)
    except OSError as exc:
        unique_defects.append(f"PLAN-ALIGNMENT-COVERAGE-REPORT-WRITE-FAILED: {exc}")
        report_path = None
        report_sha = None
    return CoverageEvaluation(
        ok=not unique_defects,
        defects=tuple(unique_defects),
        package=package,
        manifest=manifest,
        report_path=report_path,
        report_sha256=report_sha,
        bundle_sha256=bundle_sha,
        marker_sha256=marker_sha,
    )
