"""Deterministic inputs for the two L2+ product-altitude probe seats."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from harnessd import instruction_calibration, notary, review_dispatch, store


SCHEMA_VERSION = 1
MISSING_JOURNEYS_DEFECT = "MISSING-PRODUCT-PROBE-JOURNEY-ROSTER"
MALFORMED_JOURNEYS_DEFECT = "MALFORMED-PRODUCT-PROBE-JOURNEY-ROSTER"
STALE_INPUT_DEFECT = "STALE-PRODUCT-PROBE-INPUT"
FACE_NO_INVOCATION = "FACE-NO-INVOCATION"
UNCALIBRATED_DEFECT = "PRODUCT-PROBE-INSTRUCTIONS-UNCALIBRATED"
MODEL_UNCALIBRATED_DEFECT = "PRODUCT-PROBE-MODEL-UNCALIBRATED"
PROBE_SLUGS = ("user-simulation", "performance-robustness")

_HARNESS_ROOT = Path(__file__).resolve().parents[1]
_INSTRUCTION_ROOT = _HARNESS_ROOT / "operational" / "review-checks"
INSTRUCTION_PATHS = {
    "user-simulation": _INSTRUCTION_ROOT / "user-simulation.md",
    "performance-robustness": _INSTRUCTION_ROOT / "performance-robustness.md",
}
_SECTION = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_REQUIREMENT_ID = re.compile(r"^R-\d+(?:\.\d+)*$")
_JOURNEY_ID = re.compile(r"^J-\d+(?:\.\d+)*$")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_EXPECTED_JOURNEY_FIELDS = (
    "journey id",
    "kind",
    "requirement ids",
    "starting state",
    "action through the face",
    "observable result",
    "mnf obligation",
    "reflect-back status",
)
_FACE_SECTION_TITLES = {
    "invocation",
    "run",
    "running",
    "usage",
    "quick start",
    "quickstart",
}


@dataclass(frozen=True)
class ProbeRosterPreparation:
    ok: bool
    defects: tuple[str, ...]
    path: Optional[Path] = None
    sha256: Optional[str] = None


@dataclass(frozen=True)
class DisposableInstancePreparation:
    ok: bool
    defects: tuple[str, ...]
    root: Optional[Path] = None
    manifest: Optional[Path] = None
    manifest_sha256: Optional[str] = None


def instruction_calibration_defects(
    *,
    allow_uncalibrated: bool = False,
) -> tuple[str, ...]:
    return instruction_calibration.instruction_calibration_defects(
        instruction_paths=INSTRUCTION_PATHS.values(),
        required_channel="product_probe_calibration",
        defect_code=UNCALIBRATED_DEFECT,
        allow_uncalibrated=allow_uncalibrated,
    )


def _load_json(path: Path, *, label: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is unreadable or malformed: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


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
    return [
        _strip_cell(cell.replace(r"\|", "|"))
        for cell in re.split(r"(?<!\\)\|", body[1:-1])
    ]


def _section_text(text: str, title: str) -> Optional[str]:
    matches = list(_SECTION.finditer(text or ""))
    wanted = title.casefold()
    found: list[str] = []
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != wanted:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found.append(text[start:end])
    return found[0] if len(found) == 1 else None


def _intent_journeys(
    intent_path: Path,
    coverage_requirements: Mapping[str, Mapping],
) -> tuple[list[dict], list[str]]:
    text = intent_path.read_text(encoding="utf-8", errors="replace")
    section = _section_text(text, "Intent Journeys")
    if section is None:
        return [], [MISSING_JOURNEYS_DEFECT]
    lines = section.splitlines()
    header_index: Optional[int] = None
    header: list[str] = []
    for index, raw in enumerate(lines):
        cells = _split_markdown_row(raw)
        if tuple(cell.casefold() for cell in cells) == _EXPECTED_JOURNEY_FIELDS:
            header_index = index
            header = [cell.casefold() for cell in cells]
            break
    if header_index is None or header_index + 1 >= len(lines):
        return [], [f"{MALFORMED_JOURNEYS_DEFECT}: canonical table header is absent"]
    separator = _split_markdown_row(lines[header_index + 1])
    if len(separator) != len(header) or not all(
        _TABLE_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in separator
    ):
        return [], [f"{MALFORMED_JOURNEYS_DEFECT}: table separator is malformed"]

    rows: list[dict] = []
    defects: list[str] = []
    seen: set[str] = set()
    for raw in lines[header_index + 2:]:
        cells = _split_markdown_row(raw)
        if not cells:
            if rows:
                break
            continue
        if len(cells) != len(header):
            defects.append(f"{MALFORMED_JOURNEYS_DEFECT}: row has {len(cells)} fields")
            continue
        row = dict(zip(header, cells))
        journey_id = row["journey id"]
        kind = row["kind"].casefold()
        requirement_ids = tuple(
            item.strip()
            for item in re.split(r"[,; ]+", row["requirement ids"])
            if item.strip()
        )
        mnf = row["mnf obligation"].strip()
        if not _JOURNEY_ID.fullmatch(journey_id) or journey_id in seen:
            defects.append(f"{MALFORMED_JOURNEYS_DEFECT}: invalid/duplicate {journey_id!r}")
            continue
        seen.add(journey_id)
        if kind not in {"positive", "negative"}:
            defects.append(f"{MALFORMED_JOURNEYS_DEFECT}: {journey_id} has invalid kind")
        if not requirement_ids:
            defects.append(f"{MALFORMED_JOURNEYS_DEFECT}: {journey_id} has no requirements")
        for requirement_id in requirement_ids:
            requirement = coverage_requirements.get(requirement_id)
            if (
                not _REQUIREMENT_ID.fullmatch(requirement_id)
                or not requirement
                or requirement.get("tag") == "deferred"
            ):
                defects.append(
                    f"{MALFORMED_JOURNEYS_DEFECT}: {journey_id} names non-live "
                    f"requirement {requirement_id}"
                )
        if row["reflect-back status"].casefold() != "confirmed":
            defects.append(
                f"{MALFORMED_JOURNEYS_DEFECT}: {journey_id} is not reflect-back confirmed"
            )
        normalized_mnf: Optional[str] = None
        if mnf not in {"", "-", "—"}:
            requirement = coverage_requirements.get(mnf)
            if not requirement or requirement.get("must_never_fail") is not True:
                defects.append(
                    f"{MALFORMED_JOURNEYS_DEFECT}: {journey_id} names invalid MNF {mnf}"
                )
            else:
                normalized_mnf = mnf
        for key in ("starting state", "action through the face", "observable result"):
            if not row[key].strip():
                defects.append(f"{MALFORMED_JOURNEYS_DEFECT}: {journey_id} has empty {key}")
        rows.append(
            {
                "journey_id": journey_id,
                "kind": kind,
                "requirement_ids": list(requirement_ids),
                "starting_state": row["starting state"],
                "action_through_face": row["action through the face"],
                "observable_result": row["observable result"],
                "mnf_obligation": normalized_mnf,
                "reflect_back_status": "confirmed",
            }
        )
    if not rows:
        defects.append(f"{MALFORMED_JOURNEYS_DEFECT}: no confirmed journey rows")
    return rows, list(dict.fromkeys(defects))


def _intent_requirement_surfaces(
    intent_path: Path,
    coverage_requirements: Mapping[str, Mapping],
) -> tuple[list[dict], list[str]]:
    """Return exact live requirement text for frozen-anchor/threshold reads."""

    text = intent_path.read_text(encoding="utf-8", errors="replace")
    section = _section_text(text, "Requirements")
    if section is None:
        return [], [f"{STALE_INPUT_DEFECT}: canonical Requirements section is absent"]
    lines = section.splitlines()
    header_index: Optional[int] = None
    header: list[str] = []
    required = {"id", "requirement", "tag", "mnf"}
    for index, raw in enumerate(lines):
        cells = _split_markdown_row(raw)
        normalized = [cell.casefold() for cell in cells]
        if required.issubset(normalized):
            header_index = index
            header = normalized
            break
    if header_index is None or header_index + 1 >= len(lines):
        return [], [f"{STALE_INPUT_DEFECT}: canonical Requirements table is absent"]
    separator = _split_markdown_row(lines[header_index + 1])
    if len(separator) != len(header) or not all(
        _TABLE_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in separator
    ):
        return [], [f"{STALE_INPUT_DEFECT}: Requirements table separator is malformed"]

    rows_by_id: dict[str, dict] = {}
    for raw in lines[header_index + 2:]:
        cells = _split_markdown_row(raw)
        if not cells:
            if rows_by_id:
                break
            continue
        if len(cells) != len(header):
            return [], [f"{STALE_INPUT_DEFECT}: malformed Requirements table row"]
        row = dict(zip(header, cells))
        requirement_id = row["id"]
        if requirement_id in coverage_requirements:
            rows_by_id[requirement_id] = row

    defects: list[str] = []
    surfaces: list[dict] = []
    for requirement_id, coverage in sorted(coverage_requirements.items()):
        if coverage.get("tag") == "deferred":
            continue
        row = rows_by_id.get(requirement_id)
        if row is None or not row.get("requirement", "").strip():
            defects.append(
                f"{STALE_INPUT_DEFECT}: live requirement {requirement_id} has no exact text"
            )
            continue
        if row.get("tag", "").casefold() != str(coverage.get("tag") or "").casefold():
            defects.append(
                f"{STALE_INPUT_DEFECT}: tag drift for requirement {requirement_id}"
            )
            continue
        surfaces.append(
            {
                "id": requirement_id,
                "text": row["requirement"],
                "tag": str(coverage.get("tag") or ""),
                "must_never_fail": coverage.get("must_never_fail") is True,
            }
        )
    return surfaces, defects


def _checked_receipt(binding: Mapping, key: str) -> tuple[Path, dict]:
    receipt = binding.get(key)
    if not isinstance(receipt, dict):
        raise ValueError(f"missing {key}")
    checked = notary.check(receipt)
    if not checked.ok:
        raise ValueError(f"stale {key}")
    path = Path(str(receipt.get("artifact") or "")).resolve()
    if not path.is_file():
        raise ValueError(f"{key} artifact is absent: {path}")
    return path, receipt


def _checked_coverage(binding: Mapping) -> tuple[Path, dict]:
    path = Path(str(binding.get("plan_alignment_coverage_report") or ""))
    expected = str(binding.get("plan_alignment_coverage_report_sha256") or "")
    if (
        not path.is_file()
        or not expected
        or not notary.check({"sha256": expected}, target=path)
    ):
        raise ValueError("current Q3 coverage report is absent or stale")
    payload = _load_json(path, label="Q3 coverage report")
    if payload.get("status") != "pass":
        raise ValueError("Q3 coverage report did not pass")
    return path, payload


def _manifest_identity(binding: Mapping) -> tuple[Path, str, Path]:
    manifest = Path(str(binding.get("gate_candidate_artifact_manifest") or ""))
    expected = str(binding.get("gate_candidate_artifact_manifest_sha256") or "")
    snapshot = Path(str(binding.get("gate_candidate_artifact_snapshot_dir") or ""))
    if (
        not manifest.is_file()
        or not expected
        or not snapshot.is_dir()
        or not notary.check({"sha256": expected}, target=manifest)
    ):
        raise ValueError("candidate manifest/snapshot identity is absent or stale")
    return manifest, expected, snapshot


def _face_sections(text: str) -> list[str]:
    matches = list(_SECTION.finditer(text or ""))
    sections: list[str] = []
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() not in _FACE_SECTION_TITLES:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(text[start:end])
    return sections


def _declared_invocations(snapshot_root: Path) -> list[str]:
    commands = list(review_dispatch._declared_verification_commands(snapshot_root))
    candidates: list[tuple[Path, bool]] = []
    for name in ("README.md", "README.MD", "readme.md"):
        path = snapshot_root / name
        if path.is_file():
            candidates.append((path, False))
    try:
        candidates.extend(
            (path, True)
            for path in sorted(snapshot_root.rglob("live-scenario.md"))
            if path.is_file()
        )
    except OSError:
        pass
    for path, whole_file in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        sections = [text] if whole_file else _face_sections(text)
        for section in sections:
            for command in review_dispatch._commands_from_verification_section(section):
                if command not in commands:
                    commands.append(command)
    return commands


def _canonical_text(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def prepare_probe_roster(
    *,
    producer_address: str,
    producer_binding: Mapping,
    gate_dir: Path,
) -> ProbeRosterPreparation:
    try:
        intent_path, intent_receipt = _checked_receipt(
            producer_binding,
            "intent_spec_receipt",
        )
        coverage_path, coverage = _checked_coverage(producer_binding)
        manifest_path, manifest_sha, snapshot_root = _manifest_identity(producer_binding)
        manifest_defects = review_dispatch.candidate_artifact_manifest_defects_for_review(
            {"gate_for": producer_address}
        )
        if manifest_defects:
            raise ValueError("; ".join(manifest_defects))
    except (OSError, TypeError, ValueError) as exc:
        return ProbeRosterPreparation(
            ok=False,
            defects=(f"{STALE_INPUT_DEFECT}: {exc}",),
        )
    intent_fingerprint = str(
        intent_receipt.get("fingerprint")
        or (intent_receipt.get("stamp") or {}).get("sha256")
        or ""
    )
    if coverage.get("intent_fingerprint") != intent_fingerprint:
        return ProbeRosterPreparation(
            ok=False,
            defects=(f"{STALE_INPUT_DEFECT}: Q3 intent fingerprint does not match",),
        )
    requirements = {
        str(row.get("id")): row
        for row in coverage.get("requirements") or []
        if isinstance(row, dict) and row.get("id")
    }
    journeys, defects = _intent_journeys(intent_path, requirements)
    requirement_surfaces, requirement_defects = _intent_requirement_surfaces(
        intent_path,
        requirements,
    )
    defects.extend(requirement_defects)
    if defects:
        return ProbeRosterPreparation(ok=False, defects=tuple(defects))
    mnf_rows = [
        {
            "requirement_id": requirement_id,
            "failure_path_test_ids": sorted(
                str(value) for value in row.get("failure_path_test_ids") or []
            ),
        }
        for requirement_id, row in sorted(requirements.items())
        if row.get("tag") != "deferred" and row.get("must_never_fail") is True
    ]
    invocations = _declared_invocations(snapshot_root)
    face_findings = []
    if not invocations:
        face_findings.append(
            {
                "defect": FACE_NO_INVOCATION,
                "blocking": True,
                "anchor_kind": "artifact-face-promise",
                "anchor_pointer": str(manifest_path),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "producer_address": producer_address,
        "intent_spec": str(intent_path),
        "intent_fingerprint": intent_fingerprint,
        "coverage_report": str(coverage_path),
        "coverage_report_sha256": producer_binding.get(
            "plan_alignment_coverage_report_sha256"
        ),
        "candidate_manifest": str(manifest_path),
        "candidate_manifest_sha256": manifest_sha,
        "candidate_snapshot_root": str(snapshot_root),
        "requirements": requirement_surfaces,
        "journeys": journeys,
        "mnf_failure_paths": mnf_rows,
        "invocation_commands": invocations,
        "face_findings": face_findings,
    }
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    path = Path(gate_dir) / f"product-probe-roster.{identity}.json"
    content = _canonical_text(payload)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            return ProbeRosterPreparation(
                ok=False,
                defects=(f"{STALE_INPUT_DEFECT}: {exc}",),
            )
        if existing != content:
            return ProbeRosterPreparation(
                ok=False,
                defects=(f"{STALE_INPUT_DEFECT}: roster identity collision",),
            )
    else:
        store.atomic_replace(path, lambda handle: handle.write(content))
    stamped = notary.stamp(path, read_only=True)
    return ProbeRosterPreparation(
        ok=True,
        defects=(),
        path=path,
        sha256=str(stamped.get("sha256") or ""),
    )


def prepare_disposable_instance(
    *,
    producer_address: str,
    producer_binding: Mapping,
    gate_dir: Path,
    probe_slug: str,
) -> DisposableInstancePreparation:
    if probe_slug not in PROBE_SLUGS:
        return DisposableInstancePreparation(
            ok=False,
            defects=(f"UNKNOWN-PRODUCT-PROBE: {probe_slug}",),
        )
    try:
        manifest_path, manifest_sha, snapshot_root = _manifest_identity(producer_binding)
        manifest = _load_json(manifest_path, label="candidate artifact manifest")
        manifest_defects = review_dispatch.candidate_artifact_manifest_defects_for_review(
            {"gate_for": producer_address}
        )
        if manifest_defects:
            raise ValueError("; ".join(manifest_defects))
    except (OSError, TypeError, ValueError) as exc:
        return DisposableInstancePreparation(
            ok=False,
            defects=(f"{STALE_INPUT_DEFECT}: {exc}",),
        )
    root = Path(gate_dir) / "reviewers" / probe_slug / "disposable-instance"
    instance_manifest = (
        Path(gate_dir) / "probe-instance-manifests" / f"{probe_slug}.json"
    )
    if instance_manifest.is_file():
        try:
            existing = _load_json(instance_manifest, label="probe instance manifest")
        except ValueError as exc:
            return DisposableInstancePreparation(ok=False, defects=(str(exc),))
        if (
            existing.get("candidate_manifest_sha256") != manifest_sha
            or existing.get("instance_root") != str(root)
        ):
            return DisposableInstancePreparation(
                ok=False,
                defects=(
                    f"{STALE_INPUT_DEFECT}: disposable instance belongs to another candidate",
                ),
            )
        return DisposableInstancePreparation(
            ok=True,
            defects=(),
            root=root,
            manifest=instance_manifest,
            manifest_sha256=str(notary.stamp(instance_manifest).get("sha256") or ""),
        )

    copied: list[dict] = []
    for entry in manifest.get("artifacts") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            return DisposableInstancePreparation(
                ok=False,
                defects=(f"{STALE_INPUT_DEFECT}: malformed candidate artifact row",),
            )
        relative = Path(str(entry["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            return DisposableInstancePreparation(
                ok=False,
                defects=(f"{STALE_INPUT_DEFECT}: unsafe candidate artifact path {relative}",),
            )
        source = snapshot_root / relative
        checked = notary.check(
            {
                "present": True,
                "sha256": entry.get("sha256"),
                "bytes": entry.get("bytes"),
            },
            target=source,
        )
        if not checked.ok:
            return DisposableInstancePreparation(
                ok=False,
                defects=(f"{STALE_INPUT_DEFECT}: snapshot member drift {relative}",),
            )
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        try:
            destination.chmod((source.stat().st_mode & 0o777) | 0o600)
        except OSError:
            destination.chmod(0o600)
        copied_stamp = notary.stamp(destination)
        if copied_stamp.get("sha256") != entry.get("sha256"):
            return DisposableInstancePreparation(
                ok=False,
                defects=(f"{STALE_INPUT_DEFECT}: disposable copy mismatch {relative}",),
            )
        copied.append(
            {
                "path": relative.as_posix(),
                "source_snapshot": str(source),
                "sha256": copied_stamp.get("sha256"),
                "bytes": copied_stamp.get("bytes"),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "producer_address": producer_address,
        "probe_slug": probe_slug,
        "candidate_manifest": str(manifest_path),
        "candidate_manifest_sha256": manifest_sha,
        "instance_root": str(root),
        "files": copied,
    }
    store.atomic_replace(
        instance_manifest,
        lambda handle: handle.write(_canonical_text(payload)),
    )
    stamped = notary.stamp(instance_manifest, read_only=True)
    return DisposableInstancePreparation(
        ok=True,
        defects=(),
        root=root,
        manifest=instance_manifest,
        manifest_sha256=str(stamped.get("sha256") or ""),
    )
