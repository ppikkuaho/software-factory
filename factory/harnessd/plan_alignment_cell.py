"""Deterministic substrate for the Q4 plan-alignment semantic cell.

The cell intentionally separates two kinds of work:

* this module owns byte identities, physical input partitions, exact output shapes,
  dependency facts, evidence indexes, and the non-discretionary elevation set;
* fresh semantic seats own the judgments inside those shapes.

The Q3 coverage floor in :mod:`harnessd.plan_alignment` always runs first.  Nothing
here reconstructs that graph or treats fluent reviewer prose as control-plane
topology.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from harnessd import instruction_calibration, notary, store, traceability


SCHEMA_VERSION = 1
SEMANTIC_MANIFEST_DEFECT = "MALFORMED-PLAN-ALIGNMENT-SEMANTIC-MANIFEST"
UNCALIBRATED_DEFECT = "SEMANTIC-CELL-INSTRUCTIONS-UNCALIBRATED"
CLAIM_KINDS = frozenset({"input_output", "refusal", "boundary", "undetermined"})
UNDETERMINED_BEHAVIOR = "UNDETERMINED by these artifacts"
UNDETERMINED_GAP = "UNDETERMINED-GAP"
FINDING_TYPES = frozenset(
    {"TEST-DESIGN-SPLIT", "DRIFT", "SILENT-ASSUMPTION", "SCOPE-SHIFT"}
)
INSTRUCTION_ROOT = Path(__file__).resolve().parents[1] / "operational" / "plan-alignment"
INSTRUCTION_PATHS = {
    "reconstruction-verification": INSTRUCTION_ROOT / "reconstruction-window.md",
    "reconstruction-construction": INSTRUCTION_ROOT / "reconstruction-window.md",
    "comparator": INSTRUCTION_ROOT / "adversarial-comparator.md",
    "coherence": INSTRUCTION_ROOT / "coherence.md",
    "atomization": INSTRUCTION_ROOT / "atomization.md",
}
FIRST_WAVE_ROLES = (
    "atomization",
    "reconstruction-verification",
    "reconstruction-construction",
    "coherence",
)
COMPARATOR_ROLE = "comparator"
ALL_ROLES = (*FIRST_WAVE_ROLES, COMPARATOR_ROLE)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_REQUIREMENT = re.compile(r"\bR-\d+(?:\.\d+)*\b")
_ROOT_REQUIREMENT = re.compile(r"^(R-\d+)")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True)
class SemanticPreparation:
    ok: bool
    defects: tuple[str, ...] = ()
    node_address: str = ""
    node_dir: Optional[Path] = None
    semantic_manifest: Optional[Path] = None
    semantic_manifest_sha256: Optional[str] = None
    cell_sha256: Optional[str] = None
    cell_dir: Optional[Path] = None
    verification_artifacts: tuple[Path, ...] = ()
    construction_modules: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
    scope_requirement_ids: tuple[str, ...] = ()
    scope_prefixes: tuple[str, ...] = ()
    mnf_tests: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    intent_fingerprint: str = ""
    atomization_projection: Optional[Path] = None
    element_index: Optional[Path] = None
    control_record: Optional[Path] = None
    instruction_paths: Mapping[str, Path] = field(default_factory=dict)


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path.resolve()), str(root.resolve()))) == str(
            root.resolve()
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve_node_file(value: object, node_dir: Path, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty node-relative path")
    raw = Path(value)
    if raw.is_absolute():
        raise ValueError(f"{label} must be node-relative")
    path = node_dir / raw
    if not _inside(path, node_dir):
        raise ValueError(f"{label} escapes the L2 node")
    if not path.is_file():
        raise ValueError(f"{label} does not exist as a file: {value!r}")
    return path.resolve()


def _load_json(path: Path, *, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is unreadable or malformed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _freeze_json(path: Path, payload: dict) -> tuple[Path, str]:
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                stamped = notary.stamp(path, read_only=True)
                return path, str(stamped["sha256"])
        except OSError:
            pass
    store.atomic_replace(path, lambda handle: handle.write(content))
    stamped = notary.stamp(path, read_only=True)
    return path, str(stamped["sha256"])


def _canonical_sha(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))


def _section(text: str, title: str) -> str:
    matches = list(_HEADING.finditer(text))
    wanted = title.casefold()
    found: list[str] = []
    for index, match in enumerate(matches):
        normalized = re.sub(r"\s+", " ", match.group(2).strip()).casefold()
        normalized = normalized.replace("->", "→")
        if normalized != wanted.replace("->", "→"):
            continue
        level = len(match.group(1))
        end = len(text)
        for later in matches[index + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        found.append(text[match.end() : end].strip())
    if len(found) != 1 or not found[0]:
        raise ValueError(f"intent-spec must contain exactly one non-empty {title!r} section")
    return found[0]


def _checked_receipt(binding: Mapping, key: str, *, label: str) -> tuple[Path, dict]:
    receipt = binding.get(key)
    if not isinstance(receipt, dict):
        raise ValueError(f"missing {label} notary receipt")
    try:
        checked = notary.check(receipt)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"stale {label} notary receipt: {exc}") from exc
    if not checked.ok:
        raise ValueError(f"stale {label} notary receipt")
    try:
        path = Path(str(receipt["artifact"])).resolve()
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"malformed {label} notary receipt: {exc}") from exc
    return path, receipt


def instruction_calibration_defects(
    *,
    allow_uncalibrated: bool = False,
    instruction_paths: Optional[Iterable[Path]] = None,
) -> tuple[str, ...]:
    """Verify that calibration is a notary fact, never a config flag.

    A production instruction is accepted only when a current notary receipt exists
    beside it and that receipt carries the explicit ratification record reference.
    The sole bypass is the caller-visible test seam approved for this pass.
    """

    return instruction_calibration.instruction_calibration_defects(
        instruction_paths=instruction_paths or INSTRUCTION_PATHS.values(),
        required_channel="semantic_cell_calibration",
        defect_code=UNCALIBRATED_DEFECT,
        allow_uncalibrated=allow_uncalibrated,
    )


def _semantic_manifest(
    *,
    marker_payload: Mapping,
    node_dir: Path,
    coverage_manifest: Path,
    coverage_report: Path,
) -> tuple[
    Optional[Path],
    tuple[Path, ...],
    dict[str, tuple[Path, ...]],
    dict[Path, str],
    list[str],
]:
    defects: list[str] = []
    try:
        manifest = _resolve_node_file(
            marker_payload.get("semantic_manifest"),
            node_dir,
            label="semantic_manifest",
        )
    except ValueError as exc:
        return None, (), {}, {}, [f"{SEMANTIC_MANIFEST_DEFECT}: {exc}"]
    try:
        payload = _load_json(manifest, label="semantic_manifest")
        _load_json(coverage_manifest, label="coverage_manifest")
        report_payload = _load_json(coverage_report, label="coverage_report")
    except ValueError as exc:
        return manifest, (), {}, {}, [f"{SEMANTIC_MANIFEST_DEFECT}: {exc}"]
    expected = {"schema_version", "verification_artifacts", "construction_modules"}
    if set(payload) != expected:
        defects.append(
            f"{SEMANTIC_MANIFEST_DEFECT}: fields must be exactly {sorted(expected)}"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        defects.append(f"{SEMANTIC_MANIFEST_DEFECT}: schema_version must equal 1")

    verification: list[Path] = []
    path_modules: dict[Path, str] = {}
    rows = payload.get("verification_artifacts")
    if not isinstance(rows, list) or not rows:
        defects.append(
            f"{SEMANTIC_MANIFEST_DEFECT}: verification_artifacts must be a non-empty list"
        )
    else:
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {"path", "module"}:
                defects.append(
                    f"{SEMANTIC_MANIFEST_DEFECT}: verification_artifacts[{index}] "
                    "must contain exactly path and module"
                )
                continue
            module = row.get("module")
            if not isinstance(module, str) or not module.strip():
                defects.append(
                    f"{SEMANTIC_MANIFEST_DEFECT}: verification_artifacts[{index}].module "
                    "must be non-empty"
                )
                continue
            try:
                path = _resolve_node_file(
                    row.get("path"),
                    node_dir,
                    label=f"verification_artifacts[{index}].path",
                )
            except ValueError as exc:
                defects.append(f"{SEMANTIC_MANIFEST_DEFECT}: {exc}")
                continue
            if path.suffix.casefold() != ".md":
                defects.append(
                    f"{SEMANTIC_MANIFEST_DEFECT}: verification artifact must be Markdown: {path}"
                )
                continue
            if path in path_modules:
                defects.append(f"{SEMANTIC_MANIFEST_DEFECT}: duplicate artifact {path}")
                continue
            verification.append(path)
            path_modules[path] = module.strip()

    construction: dict[str, tuple[Path, ...]] = {}
    rows = payload.get("construction_modules")
    if not isinstance(rows, list) or not rows:
        defects.append(
            f"{SEMANTIC_MANIFEST_DEFECT}: construction_modules must be a non-empty list"
        )
    else:
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {"module", "artifacts"}:
                defects.append(
                    f"{SEMANTIC_MANIFEST_DEFECT}: construction_modules[{index}] "
                    "must contain exactly module and artifacts"
                )
                continue
            module = row.get("module")
            artifact_values = row.get("artifacts")
            if (
                not isinstance(module, str)
                or not module.strip()
                or not isinstance(artifact_values, list)
                or not artifact_values
            ):
                defects.append(
                    f"{SEMANTIC_MANIFEST_DEFECT}: construction_modules[{index}] "
                    "needs a non-empty module and artifact list"
                )
                continue
            module = module.strip()
            if module in construction:
                defects.append(
                    f"{SEMANTIC_MANIFEST_DEFECT}: duplicate construction module {module!r}"
                )
                continue
            paths: list[Path] = []
            for path_index, value in enumerate(artifact_values):
                try:
                    path = _resolve_node_file(
                        value,
                        node_dir,
                        label=(
                            f"construction_modules[{index}].artifacts[{path_index}]"
                        ),
                    )
                except ValueError as exc:
                    defects.append(f"{SEMANTIC_MANIFEST_DEFECT}: {exc}")
                    continue
                if path.suffix.casefold() != ".md":
                    defects.append(
                        f"{SEMANTIC_MANIFEST_DEFECT}: construction artifact must be "
                        f"Markdown: {path}"
                    )
                    continue
                if path in path_modules:
                    defects.append(
                        f"{SEMANTIC_MANIFEST_DEFECT}: artifact occurs in both windows: {path}"
                    )
                    continue
                path_modules[path] = module
                paths.append(path)
            construction[module] = tuple(paths)

    q3_paths: set[Path] = set()
    for index, row in enumerate(report_payload.get("artifacts") or []):
        if not isinstance(row, dict):
            continue
        try:
            value = row.get("path")
            path = Path(str(value or ""))
            if not path.is_absolute() or not _inside(path, node_dir) or not path.is_file():
                raise ValueError
            q3_paths.add(path.resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    semantic_paths = set(path_modules)
    outside_q3 = sorted(str(path) for path in semantic_paths - q3_paths)
    if outside_q3:
        defects.append(
            f"{SEMANTIC_MANIFEST_DEFECT}: semantic artifacts are absent from Q3 set: "
            + ", ".join(outside_q3)
        )

    q3_trace_bearing: set[Path] = set()
    for path in q3_paths:
        records, _errors = traceability.parse_artifact(path)
        if any(record.kind not in {"decision", "adr"} for record in records):
            q3_trace_bearing.add(path)
    missing = sorted(str(path) for path in q3_trace_bearing - semantic_paths)
    if missing:
        defects.append(
            f"{SEMANTIC_MANIFEST_DEFECT}: trace-bearing Q3 artifacts lack a window: "
            + ", ".join(missing)
        )

    for path in verification:
        records, errors = traceability.parse_artifact(path)
        for error in errors:
            defects.append(f"SEMANTIC-MALFORMED-TRACE:{path.name}:{error}")
        for record in records:
            if record.kind != "test":
                defects.append(
                    f"SEMANTIC-VERIFICATION-NONTEST:{record.id}:{record.kind}"
                )
    for paths in construction.values():
        for path in paths:
            records, errors = traceability.parse_artifact(path)
            for error in errors:
                defects.append(f"SEMANTIC-MALFORMED-TRACE:{path.name}:{error}")
            for record in records:
                if record.kind == "test":
                    defects.append(f"SEMANTIC-CONSTRUCTION-CONTAINS-TEST:{record.id}")

    return manifest, tuple(verification), construction, path_modules, defects


def _element_index(
    *,
    path_modules: Mapping[Path, str],
    intent_fingerprint: str,
) -> dict:
    rows: list[dict] = []
    for path in sorted(path_modules, key=str):
        artifact_sha = str(notary.stamp(path).get("sha256") or "")
        records, _errors = traceability.parse_artifact(path)
        for record in records:
            rows.append(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "serves": sorted(record.serves),
                    "level": record.level,
                    "node": record.node,
                    "artifact": str(path),
                    "artifact_sha256": artifact_sha,
                    "module": path_modules[path],
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "intent_fingerprint": intent_fingerprint,
        "elements": sorted(rows, key=lambda row: (row["id"], row["artifact"])),
        "trace_neighbors": _trace_neighbors(rows),
    }


def _trace_neighbors(rows: Iterable[Mapping]) -> dict[str, list[str]]:
    by_root: dict[str, set[str]] = {}
    for row in rows:
        module = str(row.get("module") or "")
        roots = {
            match.group(1)
            for value in [str(row.get("id") or ""), *list(row.get("serves") or [])]
            if (match := _ROOT_REQUIREMENT.match(value))
        }
        for root in roots:
            by_root.setdefault(root, set()).add(module)
    neighbors: dict[str, set[str]] = {}
    for modules in by_root.values():
        for module in modules:
            neighbors.setdefault(module, set()).update(modules - {module})
    return {
        module: sorted(values)
        for module, values in sorted(neighbors.items())
    }


def _coverage_facts(coverage) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], dict]:
    if coverage.report_path is None:
        raise ValueError("Q3 coverage report is absent")
    report = _load_json(Path(coverage.report_path), label="Q3 coverage report")
    requirements = tuple(
        str(row["id"])
        for row in report.get("requirements") or []
        if isinstance(row, dict) and row.get("disposition") == "pass"
    )
    mnf = {
        str(row["id"]): tuple(str(value) for value in row.get("failure_path_test_ids") or [])
        for row in report.get("requirements") or []
        if isinstance(row, dict)
        and row.get("disposition") == "pass"
        and row.get("must_never_fail") is True
    }
    return requirements, mnf, report


def _scope_prefixes(requirement_ids: Iterable[str]) -> tuple[str, ...]:
    roots: list[str] = []
    for value in requirement_ids:
        match = _ROOT_REQUIREMENT.match(str(value))
        if match and match.group(1) not in roots:
            roots.append(match.group(1))
    return tuple(roots)


def _current_prior_index(
    binding: Mapping,
    *,
    node_dir: Path,
) -> tuple[Optional[Path], Optional[dict]]:
    value = binding.get("plan_alignment_element_index")
    expected_sha = binding.get("plan_alignment_element_index_sha256")
    if not value or not expected_sha:
        return None, None
    path = Path(str(value))
    if not _inside(path, node_dir) or not path.is_file():
        return None, None
    if notary.stamp(path).get("sha256") != expected_sha:
        return None, None
    try:
        payload = _load_json(path, label="prior plan-alignment element index")
    except ValueError:
        return None, None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None, None
    return path.resolve(), payload


def _current_atomization_cache(
    binding: Mapping,
    *,
    intent_fingerprint: str,
) -> Optional[dict]:
    cache = binding.get("plan_alignment_atomization_cache")
    if not isinstance(cache, dict):
        return None
    if cache.get("intent_fingerprint") != intent_fingerprint:
        return None
    report = Path(str(cache.get("report") or ""))
    expected_sha = cache.get("report_sha256")
    if not report.is_file() or not expected_sha:
        return None
    if notary.stamp(report).get("sha256") != expected_sha:
        return None
    try:
        payload = _load_json(report, label="cached atomization report")
    except ValueError:
        return None
    if validate_atomization_report(
        payload,
        expected_intent_fingerprint=intent_fingerprint,
    ):
        return None
    return {
        "intent_fingerprint": intent_fingerprint,
        "report": str(report.resolve()),
        "report_sha256": str(expected_sha),
    }


def prepare_submission(
    *,
    node_address: str,
    node_dir: Path,
    marker_payload: Mapping,
    coverage,
    binding: Mapping,
    allow_uncalibrated: bool = False,
) -> SemanticPreparation:
    """Validate and materialize the deterministic cell inputs for one Q3 bundle."""

    node_dir = Path(node_dir).resolve()
    defects: list[str] = []
    if not getattr(coverage, "ok", False):
        defects.append("SEMANTIC-CELL-Q3-FLOOR-NOT-PASSED")
    if coverage.manifest is None:
        defects.append(f"{SEMANTIC_MANIFEST_DEFECT}: Q3 coverage manifest is absent")
        return SemanticPreparation(ok=False, defects=tuple(defects))

    manifest, verification, construction, path_modules, manifest_defects = _semantic_manifest(
        marker_payload=marker_payload,
        node_dir=node_dir,
        coverage_manifest=Path(coverage.manifest),
        coverage_report=Path(coverage.report_path),
    )
    defects.extend(manifest_defects)
    defects.extend(
        instruction_calibration_defects(allow_uncalibrated=allow_uncalibrated)
    )

    intent_path: Optional[Path] = None
    raw_path: Optional[Path] = None
    intent_receipt: dict = {}
    raw_receipt: dict = {}
    try:
        intent_path, intent_receipt = _checked_receipt(
            binding, "intent_spec_receipt", label="intent-spec"
        )
        raw_path, raw_receipt = _checked_receipt(
            binding, "raw_request_receipt", label="raw-request"
        )
        expected_brief_dir = (node_dir / "client-brief").resolve()
        if raw_path != expected_brief_dir / "raw-request.md":
            raise ValueError(
                "raw-request receipt must name <project>/client-brief/raw-request.md"
            )
        if (
            intent_path != expected_brief_dir / "intent-spec.md"
            or intent_path.parent != raw_path.parent
        ):
            raise ValueError(
                "intent-spec and raw-request must be siblings under project/client-brief"
            )
    except ValueError as exc:
        defects.append(f"SEMANTIC-ATOMIZATION-INPUT: {exc}")

    id_span = ""
    reflect_back = ""
    reflect_status = ""
    intent_text = ""
    raw_text = ""
    if intent_path is not None and raw_path is not None:
        try:
            intent_text = intent_path.read_text(encoding="utf-8")
            raw_text = raw_path.read_text(encoding="utf-8")
            id_span = _section(intent_text, "ID → intent-span map")
            reflect_back = _section(intent_text, "Reflect-back script")
            status_match = re.search(
                r"(?im)^\s*(?:reflect-back\s+)?status\s*:\s*(confirmed|pending)\s*$",
                reflect_back,
            )
            if not status_match or status_match.group(1).casefold() != "confirmed":
                raise ValueError("reflect-back script must carry Status: confirmed")
            reflect_status = status_match.group(1).casefold()
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            defects.append(f"SEMANTIC-ATOMIZATION-INPUT: {exc}")

    try:
        requirement_ids, mnf_tests, coverage_report = _coverage_facts(coverage)
    except ValueError as exc:
        defects.append(f"SEMANTIC-CELL-Q3-REPORT: {exc}")
        requirement_ids, mnf_tests, coverage_report = (), {}, {}

    if defects:
        return SemanticPreparation(
            ok=False,
            defects=_unique(defects),
            node_address=node_address,
            node_dir=node_dir,
            semantic_manifest=manifest,
            verification_artifacts=verification,
            construction_modules=construction,
            scope_requirement_ids=requirement_ids,
            scope_prefixes=_scope_prefixes(requirement_ids),
            mnf_tests=mnf_tests,
        )

    semantic_stamp = notary.stamp(manifest)
    semantic_sha = str(semantic_stamp["sha256"])
    intent_fingerprint = str(
        intent_receipt.get("fingerprint")
        or (intent_receipt.get("stamp") or {}).get("sha256")
        or ""
    )
    cell_sha = _canonical_sha(
        {
            "q3_bundle_sha256": coverage.bundle_sha256,
            "semantic_manifest_sha256": semantic_sha,
            "intent_fingerprint": intent_fingerprint,
        }
    )
    cell_dir = node_dir / "plan-alignment" / cell_sha[:16]
    control_dir = cell_dir / "control"
    projection_payload = {
        "schema_version": SCHEMA_VERSION,
        "intent_fingerprint": intent_fingerprint,
        "raw_request": {
            "path": str(raw_path),
            "sha256": (raw_receipt.get("stamp") or {}).get("sha256"),
            "text": raw_text,
        },
        "id_to_span": {
            "path": str(intent_path),
            "sha256": (intent_receipt.get("stamp") or {}).get("sha256"),
            "text": id_span,
        },
        "reflect_back": {
            "path": str(intent_path),
            "sha256": (intent_receipt.get("stamp") or {}).get("sha256"),
            "status": reflect_status,
            "text": reflect_back,
        },
    }
    projection, projection_sha = _freeze_json(
        control_dir / "atomization-input.json", projection_payload
    )
    element_payload = _element_index(
        path_modules=path_modules,
        intent_fingerprint=intent_fingerprint,
    )
    element_index, element_sha = _freeze_json(
        control_dir / "element-module-index.json", element_payload
    )
    full_requirement_ids = requirement_ids
    full_mnf_tests = mnf_tests
    full_verification = verification
    full_construction = construction
    prior_index_path, prior_index = _current_prior_index(
        binding,
        node_dir=node_dir,
    )
    prior_intent_fingerprint = str(
        binding.get("plan_alignment_intent_fingerprint") or ""
    )
    gating_mode = "full_initial"
    changed_element_ids: tuple[str, ...] = ()
    scope_modules = tuple(sorted(construction))
    if prior_index is not None and prior_intent_fingerprint == intent_fingerprint:
        scope = incremental_scope(
            prior_index,
            element_payload,
            prior_neighbors=prior_index.get("trace_neighbors") or {},
        )
        changed_element_ids = tuple(scope["changed_ids"])
        if changed_element_ids:
            gating_mode = "incremental"
            scope_modules = tuple(scope["modules"])
            scoped_element_ids = set(scope["scoped_element_ids"])
            scoped_roots: set[str] = set(scope["scope_prefixes"])
            for row in element_payload.get("elements") or []:
                if row.get("id") not in scoped_element_ids:
                    continue
                for value in [
                    str(row.get("id") or ""),
                    *list(row.get("serves") or []),
                ]:
                    if match := _ROOT_REQUIREMENT.match(value):
                        scoped_roots.add(match.group(1))
            requirement_ids = tuple(
                requirement_id
                for requirement_id in full_requirement_ids
                if (
                    (match := _ROOT_REQUIREMENT.match(requirement_id))
                    and match.group(1) in scoped_roots
                )
            )
            mnf_tests = {
                requirement_id: test_ids
                for requirement_id, test_ids in full_mnf_tests.items()
                if requirement_id in requirement_ids
            }
            verification = tuple(
                path
                for path in full_verification
                if path_modules.get(path) in set(scope_modules)
            )
            construction = {
                module: paths
                for module, paths in full_construction.items()
                if module in set(scope_modules)
            }
            if not requirement_ids or not verification or not construction:
                gating_mode = "full_no_safe_increment"
                requirement_ids = full_requirement_ids
                mnf_tests = full_mnf_tests
                verification = full_verification
                construction = full_construction
                scope_modules = tuple(sorted(full_construction))
        else:
            gating_mode = "full_no_trace_delta"
    elif prior_index is not None and prior_intent_fingerprint != intent_fingerprint:
        gating_mode = "full_intent_revision"

    atomization_cache = _current_atomization_cache(
        binding,
        intent_fingerprint=intent_fingerprint,
    )
    control_payload = {
        "schema_version": SCHEMA_VERSION,
        "node_address": node_address,
        "node_dir": str(node_dir),
        "q3_bundle_sha256": coverage.bundle_sha256,
        "coverage_manifest": str(coverage.manifest),
        "coverage_report": str(coverage.report_path),
        "coverage_report_sha256": coverage.report_sha256,
        "semantic_manifest": str(manifest),
        "semantic_manifest_sha256": semantic_sha,
        "cell_sha256": cell_sha,
        "intent_spec": str(intent_path),
        "raw_request": str(raw_path),
        "intent_fingerprint": intent_fingerprint,
        "verification_artifacts": [str(path) for path in verification],
        "construction_modules": {
            module: [str(path) for path in paths]
            for module, paths in sorted(construction.items())
        },
        "gating_mode": gating_mode,
        "changed_element_ids": list(changed_element_ids),
        "scope_modules": list(scope_modules),
        "prior_element_index": (
            str(prior_index_path) if prior_index_path is not None else None
        ),
        "scope_requirement_ids": list(requirement_ids),
        "scope_prefixes": list(_scope_prefixes(requirement_ids)),
        "mnf_tests": {
            requirement_id: list(test_ids)
            for requirement_id, test_ids in sorted(mnf_tests.items())
        },
        "atomization_projection": str(projection),
        "atomization_projection_sha256": projection_sha,
        "element_index": str(element_index),
        "element_index_sha256": element_sha,
        "trace_neighbors": element_payload["trace_neighbors"],
        "atomization_cache": atomization_cache,
        "instruction_paths": {
            role: str(path.resolve()) for role, path in INSTRUCTION_PATHS.items()
        },
    }
    control_record, _control_sha = _freeze_json(
        control_dir / "cell.json", control_payload
    )
    return SemanticPreparation(
        ok=True,
        node_address=node_address,
        node_dir=node_dir,
        semantic_manifest=manifest,
        semantic_manifest_sha256=semantic_sha,
        cell_sha256=cell_sha,
        cell_dir=cell_dir,
        verification_artifacts=verification,
        construction_modules=construction,
        scope_requirement_ids=requirement_ids,
        scope_prefixes=_scope_prefixes(requirement_ids),
        mnf_tests=mnf_tests,
        intent_fingerprint=intent_fingerprint,
        atomization_projection=projection,
        element_index=element_index,
        control_record=control_record,
        instruction_paths={
            role: path.resolve() for role, path in INSTRUCTION_PATHS.items()
        },
    )


def load_control(path: Path) -> dict:
    """Load a cell control record only while its own notary-listed members are current."""

    payload = _load_json(Path(path), label="semantic cell control record")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("semantic cell control record schema_version must equal 1")
    for path_key, sha_key in (
        ("coverage_report", "coverage_report_sha256"),
        ("semantic_manifest", "semantic_manifest_sha256"),
        ("atomization_projection", "atomization_projection_sha256"),
        ("element_index", "element_index_sha256"),
    ):
        artifact = Path(str(payload.get(path_key) or ""))
        stamped = notary.stamp(artifact)
        if stamped.get("sha256") != payload.get(sha_key):
            raise ValueError(f"semantic cell control member drifted: {artifact}")
    cache = payload.get("atomization_cache")
    if cache is not None:
        if not isinstance(cache, dict):
            raise ValueError("semantic cell atomization cache is malformed")
        report = Path(str(cache.get("report") or ""))
        if (
            cache.get("intent_fingerprint") != payload.get("intent_fingerprint")
            or not report.is_file()
            or notary.stamp(report).get("sha256") != cache.get("report_sha256")
        ):
            raise ValueError("semantic cell atomization cache drifted")
    return payload


def _exact_fields(payload: object, expected: set[str], *, label: str) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{label}-NOT-OBJECT"]
    if set(payload) != expected:
        return [
            f"{label}-FIELDS: expected={sorted(expected)} got={sorted(payload)}"
        ]
    return []


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_reconstruction_report(
    payload: object,
    *,
    expected_window: str,
    expected_bundle_sha256: str,
    expected_requirement_ids: Iterable[str],
    expected_scope_prefixes: Iterable[str],
) -> tuple[str, ...]:
    label = "RECONSTRUCTION"
    defects = _exact_fields(
        payload,
        {
            "schema_version",
            "bundle_sha256",
            "window",
            "scope_prefixes",
            "claims",
            "assumptions",
        },
        label=label,
    )
    if defects:
        return tuple(defects)
    assert isinstance(payload, dict)
    if payload.get("schema_version") != SCHEMA_VERSION:
        defects.append(f"{label}-SCHEMA")
    if payload.get("bundle_sha256") != expected_bundle_sha256:
        defects.append(f"{label}-BUNDLE-DRIFT")
    if payload.get("window") != expected_window:
        defects.append(f"{label}-WINDOW-MISMATCH")
    prefixes = payload.get("scope_prefixes")
    if not isinstance(prefixes, list) or not all(_nonempty(value) for value in prefixes):
        defects.append(f"{label}-SCOPE-PREFIXES")
    elif tuple(prefixes) != tuple(expected_scope_prefixes):
        defects.append(f"{label}-SCOPE-DRIFT")
    assumptions = payload.get("assumptions")
    if not isinstance(assumptions, list) or not all(_nonempty(value) for value in assumptions):
        defects.append(f"{label}-ASSUMPTIONS")
    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        defects.append(f"{label}-CLAIMS")
        claims = []
    seen_claims: set[str] = set()
    covered: set[str] = set()
    expected_claim_fields = {
        "requirement_id",
        "claim_id",
        "behavior",
        "claim_kind",
        "missing",
        "stimulus",
        "observable",
    }
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or set(claim) != expected_claim_fields:
            defects.append(f"{label}-CLAIM-FIELDS:{index}")
            continue
        claim_id = str(claim.get("claim_id") or "")
        requirement_id = str(claim.get("requirement_id") or "")
        if not _nonempty(claim_id):
            defects.append(f"{label}-EMPTY-CLAIM-ID:{index}")
        elif claim_id in seen_claims:
            defects.append(f"{label}-DUPLICATE-CLAIM-ID:{claim_id}")
        seen_claims.add(claim_id)
        if requirement_id:
            covered.add(requirement_id)
        claim_kind = claim.get("claim_kind")
        if claim_kind not in CLAIM_KINDS:
            defects.append(f"{label}-INVALID-CLAIM-KIND:{claim_id or index}")
        if claim_kind == "undetermined":
            if claim.get("behavior") != UNDETERMINED_BEHAVIOR:
                defects.append(f"{label}-UNDETERMINED-BEHAVIOR:{claim_id or index}")
            if not _nonempty(claim.get("missing")):
                defects.append(f"{label}-UNDETERMINED-MISSING:{claim_id or index}")
            for field_name in ("stimulus", "observable"):
                if not isinstance(claim.get(field_name), str):
                    defects.append(
                        f"{label}-UNDETERMINED-{field_name.upper()}:{claim_id or index}"
                    )
        else:
            if not _nonempty(claim.get("behavior")):
                defects.append(f"{label}-EMPTY-BEHAVIOR:{claim_id or index}")
            if claim.get("missing") != "":
                defects.append(f"{label}-DETERMINED-MISSING:{claim_id or index}")
            if not _nonempty(claim.get("stimulus")):
                defects.append(f"{label}-EMPTY-STIMULUS:{claim_id or index}")
            if not _nonempty(claim.get("observable")):
                defects.append(f"{label}-EMPTY-OBSERVABLE:{claim_id or index}")
    for requirement_id in expected_requirement_ids:
        if str(requirement_id) not in covered:
            defects.append(f"{label}-MISSING-REQUIREMENT:{requirement_id}")
    return _unique(defects)


def validate_comparator_report(
    payload: object,
    *,
    expected_bundle_sha256: str,
    expected_scope_prefixes: Iterable[str],
    expected_mnf_tests: Mapping[str, Iterable[str]],
) -> tuple[str, ...]:
    label = "COMPARATOR"
    defects = _exact_fields(
        payload,
        {
            "schema_version",
            "bundle_sha256",
            "scope_prefixes",
            "window_splits",
            "intent_findings",
            "mnf_adequacy",
        },
        label=label,
    )
    if defects:
        return tuple(defects)
    assert isinstance(payload, dict)
    if payload.get("schema_version") != SCHEMA_VERSION:
        defects.append(f"{label}-SCHEMA")
    if payload.get("bundle_sha256") != expected_bundle_sha256:
        defects.append(f"{label}-BUNDLE-DRIFT")
    if tuple(payload.get("scope_prefixes") or ()) != tuple(expected_scope_prefixes):
        defects.append(f"{label}-SCOPE-DRIFT")
    splits = payload.get("window_splits")
    if not isinstance(splits, list):
        defects.append(f"{label}-WINDOW-SPLITS")
        splits = []
    for index, row in enumerate(splits):
        required = {
            "type",
            "requirement_id",
            "verification_claim_ref",
            "construction_claim_ref",
            "disagreement",
        }
        if not isinstance(row, dict) or set(row) != required:
            defects.append(f"{label}-WINDOW-SPLIT-FIELDS:{index}")
            continue
        if row.get("type") != "TEST-DESIGN-SPLIT" or not all(
            _nonempty(row.get(key))
            for key in required - {"type"}
        ):
            defects.append(f"{label}-WINDOW-SPLIT-SHAPE:{index}")
    findings = payload.get("intent_findings")
    if not isinstance(findings, list):
        defects.append(f"{label}-INTENT-FINDINGS")
        findings = []
    expected_finding_fields = {
        "type",
        "requirement_id",
        "intended_behavior",
        "reconstructed_behavior",
        "evidence_refs",
        "owning_module",
        "owning_level",
        "confidence",
    }
    for index, row in enumerate(findings):
        if not isinstance(row, dict) or set(row) != expected_finding_fields:
            defects.append(f"{label}-INTENT-FINDING-FIELDS:{index}")
            continue
        if row.get("type") not in FINDING_TYPES - {"TEST-DESIGN-SPLIT"}:
            defects.append(f"{label}-INTENT-FINDING-TYPE:{index}")
        for key in (
            "requirement_id",
            "intended_behavior",
            "reconstructed_behavior",
            "owning_module",
            "owning_level",
        ):
            if not _nonempty(row.get(key)):
                defects.append(f"{label}-INTENT-FINDING-EMPTY:{index}:{key}")
        refs = row.get("evidence_refs")
        if not isinstance(refs, list) or not refs or not all(_nonempty(ref) for ref in refs):
            defects.append(f"{label}-INTENT-FINDING-EVIDENCE:{index}")
        if row.get("confidence") not in {"high", "medium", "low"}:
            defects.append(f"{label}-INTENT-FINDING-CONFIDENCE:{index}")

    adequacy = payload.get("mnf_adequacy")
    if not isinstance(adequacy, list):
        defects.append(f"{label}-MNF-ADEQUACY")
        adequacy = []
    seen: set[tuple[str, str]] = set()
    expected_mnf_fields = {
        "requirement_id",
        "test_id",
        "failure_exercised",
        "assertion_catches",
        "adequate",
        "defect_reason",
    }
    for index, row in enumerate(adequacy):
        if not isinstance(row, dict) or set(row) != expected_mnf_fields:
            defects.append(f"{label}-MNF-FIELDS:{index}")
            continue
        key = (str(row.get("requirement_id") or ""), str(row.get("test_id") or ""))
        seen.add(key)
        if not _nonempty(row.get("failure_exercised")) or not _nonempty(
            row.get("assertion_catches")
        ):
            defects.append(f"{label}-VACUOUS-MNF:{key[0]}:{key[1]}")
        if not isinstance(row.get("adequate"), bool):
            defects.append(f"{label}-MNF-ADEQUATE-TYPE:{key[0]}:{key[1]}")
        if row.get("adequate") is False and not _nonempty(row.get("defect_reason")):
            defects.append(f"{label}-MNF-MISSING-REASON:{key[0]}:{key[1]}")
    for requirement_id, test_ids in expected_mnf_tests.items():
        for test_id in test_ids:
            if (str(requirement_id), str(test_id)) not in seen:
                defects.append(f"{label}-MNF-MISSING:{requirement_id}:{test_id}")
    return _unique(defects)


def validate_coherence_report(
    payload: object,
    *,
    expected_bundle_sha256: str,
    expected_modules: Iterable[str],
) -> tuple[str, ...]:
    label = "COHERENCE"
    defects = _exact_fields(
        payload,
        {
            "schema_version",
            "bundle_sha256",
            "modules_read",
            "shared_assumptions",
            "contradictions",
        },
        label=label,
    )
    if defects:
        return tuple(defects)
    assert isinstance(payload, dict)
    if payload.get("schema_version") != SCHEMA_VERSION:
        defects.append(f"{label}-SCHEMA")
    if payload.get("bundle_sha256") != expected_bundle_sha256:
        defects.append(f"{label}-BUNDLE-DRIFT")
    if sorted(payload.get("modules_read") or []) != sorted(expected_modules):
        defects.append(f"{label}-MODULE-SCOPE-DRIFT")
    assumptions = payload.get("shared_assumptions")
    if not isinstance(assumptions, list):
        defects.append(f"{label}-SHARED-ASSUMPTIONS")
        assumptions = []
    assumption_keys: set[str] = set()
    for index, row in enumerate(assumptions):
        fields = {
            "assumption_key",
            "modules",
            "interpretations",
            "evidence",
        }
        if not isinstance(row, dict) or set(row) != fields:
            defects.append(f"{label}-ASSUMPTION-FIELDS:{index}")
            continue
        key = str(row.get("assumption_key") or "")
        modules = row.get("modules")
        if not key or not isinstance(modules, list) or len(set(modules)) < 2:
            defects.append(f"{label}-ASSUMPTION-SHAPE:{index}")
            continue
        assumption_keys.add(key)
        for field_name in ("interpretations", "evidence"):
            values = row.get(field_name)
            if not isinstance(values, dict) or set(values) != set(modules):
                defects.append(f"{label}-ASSUMPTION-{field_name.upper()}:{key}")
    contradictions = payload.get("contradictions")
    if not isinstance(contradictions, list):
        defects.append(f"{label}-CONTRADICTIONS")
        contradictions = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for index, row in enumerate(contradictions):
        fields = {
            "type",
            "modules",
            "assumption_key",
            "incompatible_claims",
            "evidence_paths",
            "affected_trace_prefixes",
        }
        if not isinstance(row, dict) or set(row) != fields:
            defects.append(f"{label}-CONTRADICTION-FIELDS:{index}")
            continue
        modules = row.get("modules")
        key = str(row.get("assumption_key") or "")
        if (
            row.get("type") != "CONTRADICTION"
            or not isinstance(modules, list)
            or len(modules) != 2
            or len(set(modules)) != 2
            or not key
        ):
            defects.append(f"{label}-CONTRADICTION-PAIR:{index}")
            continue
        pair = tuple(sorted(str(value) for value in modules))
        identity = (pair[0], pair[1], key)
        if identity in seen_pairs:
            defects.append(f"{label}-DUPLICATE-CONTRADICTION:{'::'.join(identity)}")
        seen_pairs.add(identity)
        if key not in assumption_keys:
            defects.append(f"{label}-CONTRADICTION-UNKNOWN-ASSUMPTION:{key}")
        claims = row.get("incompatible_claims")
        evidence = row.get("evidence_paths")
        prefixes = row.get("affected_trace_prefixes")
        if not isinstance(claims, list) or len(claims) != 2 or not all(
            _nonempty(value) for value in claims
        ):
            defects.append(f"{label}-CONTRADICTION-CLAIMS:{'::'.join(identity)}")
        if not isinstance(evidence, list) or len(evidence) < 2 or not all(
            _nonempty(value) for value in evidence
        ):
            defects.append(f"{label}-CONTRADICTION-EVIDENCE:{'::'.join(identity)}")
        if not isinstance(prefixes, list) or not all(_nonempty(value) for value in prefixes):
            defects.append(f"{label}-CONTRADICTION-PREFIXES:{'::'.join(identity)}")
    return _unique(defects)


def validate_atomization_report(
    payload: object,
    *,
    expected_intent_fingerprint: str,
) -> tuple[str, ...]:
    label = "ATOMIZATION"
    defects = _exact_fields(
        payload,
        {"schema_version", "intent_fingerprint", "findings"},
        label=label,
    )
    if defects:
        return tuple(defects)
    assert isinstance(payload, dict)
    if payload.get("schema_version") != SCHEMA_VERSION:
        defects.append(f"{label}-SCHEMA")
    if payload.get("intent_fingerprint") != expected_intent_fingerprint:
        defects.append(f"{label}-INTENT-DRIFT")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        defects.append(f"{label}-FINDINGS")
        findings = []
    seen: set[str] = set()
    for index, row in enumerate(findings):
        fields = {"type", "span_id", "verbatim_span", "source", "reason"}
        if not isinstance(row, dict) or set(row) != fields:
            defects.append(f"{label}-FINDING-FIELDS:{index}")
            continue
        span_id = str(row.get("span_id") or "")
        span = str(row.get("verbatim_span") or "")
        if row.get("type") != "UNMINTED":
            defects.append(f"{label}-FINDING-TYPE:{index}")
        if not _SPAN_ID.fullmatch(span_id):
            defects.append(f"{label}-SPAN-ID:{index}")
        elif span_id != hashlib.sha256(span.encode("utf-8")).hexdigest()[:12]:
            defects.append(f"{label}-SPAN-HASH:{span_id}")
        if span_id in seen:
            defects.append(f"{label}-DUPLICATE-SPAN:{span_id}")
        seen.add(span_id)
        if not span or row.get("source") not in {"raw_request", "reflect_back"}:
            defects.append(f"{label}-SPAN-SOURCE:{span_id or index}")
        if not _nonempty(row.get("reason")):
            defects.append(f"{label}-REASON:{span_id or index}")
    return _unique(defects)


def _finding(
    finding_type: str,
    key: str,
    payload: Mapping,
) -> dict:
    canonical = {
        "type": finding_type,
        "key": key,
        "evidence": dict(payload),
    }
    canonical["fingerprint"] = _canonical_sha(canonical)
    return canonical


def _serves_requirement(row: Mapping, requirement_id: str) -> bool:
    return str(row.get("id") or "") == requirement_id or requirement_id in {
        str(value) for value in row.get("serves") or []
    }


def _undetermined_elevations(
    *,
    reconstruction_reports: Mapping[str, Mapping],
    element_index: Mapping,
) -> list[dict]:
    elements = [
        row
        for row in element_index.get("elements") or []
        if isinstance(row, Mapping)
    ]
    rows: list[dict] = []
    role_windows = {
        "reconstruction-verification": ("verification", "test"),
        "reconstruction-construction": ("construction", "design"),
    }
    for role, (window, artifact_kind) in role_windows.items():
        report = reconstruction_reports.get(role) or {}
        for claim in report.get("claims") or []:
            if not isinstance(claim, Mapping) or claim.get("claim_kind") != "undetermined":
                continue
            requirement_id = str(claim.get("requirement_id") or "")
            claim_id = str(claim.get("claim_id") or "")
            levels = sorted(
                {
                    str(row.get("level") or "").strip()
                    for row in elements
                    if row.get("kind") == artifact_kind
                    and _serves_requirement(row, requirement_id)
                    and str(row.get("level") or "").strip()
                }
            )
            routing_defect = None
            if len(levels) == 1:
                owning_level = levels[0]
            else:
                owning_level = "UNRESOLVED"
                ambiguity = "ZERO" if not levels else "MULTIPLE"
                routing_defect = (
                    f"UNDETERMINED-GAP-OWNING-LEVEL-{ambiguity}:"
                    f"{window}:{requirement_id}:{claim_id}"
                )
            evidence = {
                "requirement_id": requirement_id,
                "claim_id": claim_id,
                "behavior": str(claim.get("behavior") or ""),
                "missing": str(claim.get("missing") or ""),
                "source_role": role,
                "source_window": window,
                "owning_level": owning_level,
                "owning_level_candidates": levels,
            }
            if routing_defect is not None:
                evidence["routing_defect"] = routing_defect
            rows.append(
                _finding(
                    UNDETERMINED_GAP,
                    f"{requirement_id}::{window}::{claim_id}",
                    evidence,
                )
            )
    return rows


def required_elevations(
    *,
    comparator_report: Mapping,
    coherence_report: Mapping,
    atomization_report: Mapping,
    reconstruction_reports: Optional[Mapping[str, Mapping]] = None,
    element_index: Optional[Mapping] = None,
) -> list[dict]:
    """Return the exact, non-discretionary elevate-only finding set."""

    rows = _undetermined_elevations(
        reconstruction_reports=reconstruction_reports or {},
        element_index=element_index or {},
    )
    for row in comparator_report.get("window_splits") or []:
        if isinstance(row, dict) and row.get("type") == "TEST-DESIGN-SPLIT":
            rows.append(
                _finding(
                    "TEST-DESIGN-SPLIT",
                    str(row.get("requirement_id") or ""),
                    row,
                )
            )
    for row in comparator_report.get("intent_findings") or []:
        if isinstance(row, dict) and row.get("type") in FINDING_TYPES:
            rows.append(
                _finding(
                    str(row["type"]),
                    str(row.get("requirement_id") or ""),
                    row,
                )
            )
    for row in comparator_report.get("mnf_adequacy") or []:
        if not isinstance(row, dict):
            continue
        vacuous = not _nonempty(row.get("failure_exercised")) or not _nonempty(
            row.get("assertion_catches")
        )
        if row.get("adequate") is False or vacuous:
            key = f"{row.get('requirement_id')}::{row.get('test_id')}"
            rows.append(_finding("MNF-ADEQUACY", key, row))
    for row in coherence_report.get("contradictions") or []:
        if not isinstance(row, dict) or row.get("type") != "CONTRADICTION":
            continue
        modules = sorted(str(value) for value in row.get("modules") or [])
        key = "::".join([*modules, str(row.get("assumption_key") or "")])
        rows.append(_finding("CONTRADICTION", key, row))
    for row in atomization_report.get("findings") or []:
        if isinstance(row, dict) and row.get("type") == "UNMINTED":
            rows.append(
                _finding(
                    "UNMINTED",
                    f"UNMINTED-{row.get('span_id')}",
                    row,
                )
            )
    deduped: dict[str, dict] = {}
    for row in rows:
        deduped[row["fingerprint"]] = row
    return sorted(
        deduped.values(),
        key=lambda row: (row["type"], row["key"], row["fingerprint"]),
    )


def elevation_delta(
    prior: Iterable[Mapping],
    current: Iterable[Mapping],
) -> dict[str, list[dict]]:
    """Return stable new/changed/cleared findings for the next L1 wake."""

    prior_by_fingerprint = {
        str(row.get("fingerprint")): dict(row)
        for row in prior
        if isinstance(row, Mapping) and row.get("fingerprint")
    }
    current_by_fingerprint = {
        str(row.get("fingerprint")): dict(row)
        for row in current
        if isinstance(row, Mapping) and row.get("fingerprint")
    }
    prior_by_key = {
        (str(row.get("type")), str(row.get("key"))): row
        for row in prior_by_fingerprint.values()
    }
    current_by_key = {
        (str(row.get("type")), str(row.get("key"))): row
        for row in current_by_fingerprint.values()
    }
    changed_keys = {
        key
        for key in set(prior_by_key) & set(current_by_key)
        if prior_by_key[key].get("fingerprint")
        != current_by_key[key].get("fingerprint")
    }
    return {
        "new": sorted(
            [
                row
                for fingerprint, row in current_by_fingerprint.items()
                if fingerprint not in prior_by_fingerprint
                and (str(row.get("type")), str(row.get("key")))
                not in changed_keys
            ],
            key=lambda row: (row.get("type"), row.get("key"), row.get("fingerprint")),
        ),
        "changed": sorted(
            [current_by_key[key] for key in changed_keys],
            key=lambda row: (row.get("type"), row.get("key"), row.get("fingerprint")),
        ),
        "cleared": sorted(
            [
                row
                for fingerprint, row in prior_by_fingerprint.items()
                if fingerprint not in current_by_fingerprint
                and (str(row.get("type")), str(row.get("key")))
                not in changed_keys
            ],
            key=lambda row: (row.get("type"), row.get("key"), row.get("fingerprint")),
        ),
    }


def _row_identity(row: Mapping) -> str:
    return _canonical_sha(dict(row))


def incremental_scope(
    prior_index: Mapping,
    current_index: Mapping,
    *,
    prior_neighbors: Mapping[str, Iterable[str]],
    seat_assumption_claims: Optional[Mapping[str, Iterable[str]]] = None,
) -> dict:
    """Compute the touched subtree and coherence neighborhood from trace facts only."""

    # The parameter is intentionally accepted and ignored so tests/callers can prove that reviewer
    # prose is not a topology input.
    del seat_assumption_claims
    prior_rows = {
        str(row.get("id")): row
        for row in prior_index.get("elements") or []
        if isinstance(row, dict) and row.get("id")
    }
    current_rows = {
        str(row.get("id")): row
        for row in current_index.get("elements") or []
        if isinstance(row, dict) and row.get("id")
    }
    changed_ids = sorted(
        element_id
        for element_id in set(prior_rows) | set(current_rows)
        if element_id not in prior_rows
        or element_id not in current_rows
        or _row_identity(prior_rows[element_id]) != _row_identity(current_rows[element_id])
    )
    changed_rows = [
        current_rows.get(element_id) or prior_rows[element_id]
        for element_id in changed_ids
    ]
    prefixes: set[str] = set()
    changed_modules: set[str] = set()
    for row in changed_rows:
        changed_modules.add(str(row.get("module") or ""))
        for value in [str(row.get("id") or ""), *list(row.get("serves") or [])]:
            if match := _ROOT_REQUIREMENT.match(value):
                prefixes.add(match.group(1))

    current_neighbors = _trace_neighbors(current_rows.values())
    modules = set(changed_modules)
    for module in list(changed_modules):
        modules.update(str(value) for value in prior_neighbors.get(module, ()))
        modules.update(str(value) for value in current_neighbors.get(module, ()))
    modules.discard("")
    scoped_ids: set[str] = set()
    for row in current_rows.values():
        row_roots = {
            match.group(1)
            for value in [str(row.get("id") or ""), *list(row.get("serves") or [])]
            if (match := _ROOT_REQUIREMENT.match(value))
        }
        if str(row.get("module") or "") in modules or row_roots & prefixes:
            scoped_ids.add(str(row.get("id")))
    return {
        "changed_ids": changed_ids,
        "scope_prefixes": sorted(prefixes),
        "modules": sorted(modules),
        "scoped_element_ids": sorted(scoped_ids),
        "trace_neighbors": current_neighbors,
    }


def report_defects_for_role(
    role: str,
    payload: object,
    *,
    control: Mapping,
) -> tuple[str, ...]:
    """Route one assigned seat output through its exact deterministic shape."""

    if role == "reconstruction-verification":
        return validate_reconstruction_report(
            payload,
            expected_window="verification",
            expected_bundle_sha256=str(control["cell_sha256"]),
            expected_requirement_ids=control.get("scope_requirement_ids") or [],
            expected_scope_prefixes=control.get("scope_prefixes") or [],
        )
    if role == "reconstruction-construction":
        return validate_reconstruction_report(
            payload,
            expected_window="construction",
            expected_bundle_sha256=str(control["cell_sha256"]),
            expected_requirement_ids=control.get("scope_requirement_ids") or [],
            expected_scope_prefixes=control.get("scope_prefixes") or [],
        )
    if role == "comparator":
        return validate_comparator_report(
            payload,
            expected_bundle_sha256=str(control["cell_sha256"]),
            expected_scope_prefixes=control.get("scope_prefixes") or [],
            expected_mnf_tests=control.get("mnf_tests") or {},
        )
    if role == "coherence":
        return validate_coherence_report(
            payload,
            expected_bundle_sha256=str(control["cell_sha256"]),
            expected_modules=(control.get("construction_modules") or {}).keys(),
        )
    if role == "atomization":
        return validate_atomization_report(
            payload,
            expected_intent_fingerprint=str(control["intent_fingerprint"]),
        )
    return (f"SEMANTIC-UNKNOWN-ROLE:{role}",)


def validate_report_path(
    role: str,
    report_path: Path,
    *,
    control_path: Path,
) -> tuple[str, ...]:
    try:
        control = load_control(control_path)
        payload = _load_json(Path(report_path), label=f"{role} report")
    except ValueError as exc:
        return (f"SEMANTIC-SEAT-REPORT:{role}:{exc}",)
    return report_defects_for_role(role, payload, control=control)


def seat_address(node_address: str, cell_sha256: str, role: str) -> str:
    if role not in ALL_ROLES:
        raise ValueError(f"unknown semantic cell role {role!r}")
    node_path = node_address.split("#", 1)[0].strip("/")
    return (
        f"{node_path}/plan-alignment/{cell_sha256[:16]}/seats/{role}"
        "#exec"
    )


def input_paths_for_role(
    role: str,
    *,
    control: Mapping,
    report_paths: Optional[Mapping[str, Path]] = None,
) -> tuple[Path, ...]:
    """Return the exact physical input window for one role."""

    report_paths = report_paths or {}
    verification = tuple(
        Path(str(value)).resolve()
        for value in control.get("verification_artifacts") or []
    )
    construction = tuple(
        Path(str(value)).resolve()
        for paths in (control.get("construction_modules") or {}).values()
        for value in paths
    )
    if role == "reconstruction-verification":
        return verification
    if role == "reconstruction-construction":
        return construction
    if role == "coherence":
        return construction
    if role == "atomization":
        return (
            Path(str(control["atomization_projection"])).resolve(),
            Path(str(control["raw_request"])).resolve(),
        )
    if role == "comparator":
        required_test_ids = {
            str(test_id)
            for test_ids in (control.get("mnf_tests") or {}).values()
            for test_id in test_ids
        }
        mnf_files: list[Path] = []
        for path in verification:
            records, _errors = traceability.parse_artifact(path)
            if any(record.id in required_test_ids for record in records):
                mnf_files.append(path)
        paths = [
            Path(str(control["intent_spec"])).resolve(),
            Path(str(control["coverage_report"])).resolve(),
            Path(str(report_paths["reconstruction-verification"])).resolve(),
            Path(str(report_paths["reconstruction-construction"])).resolve(),
            *mnf_files,
        ]
        return tuple(dict.fromkeys(paths))
    raise ValueError(f"unknown semantic cell role {role!r}")


def write_input_manifest(
    seat_dir: Path,
    *,
    role: str,
    control: Mapping,
    paths: Iterable[Path],
) -> Path:
    """Freeze the daemon-authored exact-read roster inside an isolated seat workroot."""

    rows: list[dict] = []
    for path in dict.fromkeys(Path(path).resolve() for path in paths):
        stamped = notary.stamp(path)
        if stamped.get("present") is not True or not stamped.get("sha256"):
            raise ValueError(f"semantic exact input is absent or unstamped: {path}")
        rows.append(
            {
                "path": str(path),
                "sha256": stamped["sha256"],
                "bytes": stamped.get("bytes"),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "cell_sha256": control["cell_sha256"],
        "scope_requirement_ids": list(control.get("scope_requirement_ids") or []),
        "scope_prefixes": list(control.get("scope_prefixes") or []),
        "construction_modules": sorted(
            (control.get("construction_modules") or {}).keys()
        ),
        "mnf_tests": control.get("mnf_tests") or {},
        "inputs": rows,
    }
    path, _sha = _freeze_json(Path(seat_dir) / "input-manifest.json", payload)
    return path


def validate_input_manifest(path: Path) -> tuple[str, ...]:
    try:
        payload = _load_json(Path(path), label="semantic input manifest")
    except ValueError as exc:
        return (f"SEMANTIC-INPUT-MANIFEST:{exc}",)
    expected = {
        "schema_version",
        "role",
        "cell_sha256",
        "scope_requirement_ids",
        "scope_prefixes",
        "construction_modules",
        "mnf_tests",
        "inputs",
    }
    defects = _exact_fields(payload, expected, label="SEMANTIC-INPUT-MANIFEST")
    if defects:
        return tuple(defects)
    for index, row in enumerate(payload.get("inputs") or []):
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            defects.append(f"SEMANTIC-INPUT-MANIFEST-ROW:{index}")
            continue
        path_value = Path(str(row.get("path") or ""))
        if not path_value.is_absolute():
            defects.append(f"SEMANTIC-INPUT-MANIFEST-PATH:{index}")
            continue
        current = notary.stamp(path_value)
        if current.get("sha256") != row.get("sha256"):
            defects.append(f"SEMANTIC-INPUT-DRIFT:{path_value}")
    return _unique(defects)


def build_evidence_index(
    *,
    control_path: Path,
    report_paths: Mapping[str, Path],
) -> tuple[Optional[Path], Optional[str], tuple[str, ...]]:
    """Validate, stamp, and join all five seat reports into one cell evidence index."""

    try:
        control = load_control(control_path)
    except ValueError as exc:
        return None, None, (f"SEMANTIC-CELL-CONTROL:{exc}",)
    defects: list[str] = []
    payloads: dict[str, dict] = {}
    report_rows: dict[str, dict] = {}
    for role in ALL_ROLES:
        report_path = Path(str(report_paths.get(role) or ""))
        role_defects = validate_report_path(
            role,
            report_path,
            control_path=control_path,
        )
        defects.extend(role_defects)
        try:
            payloads[role] = _load_json(report_path, label=f"{role} report")
        except ValueError:
            continue
        stamped = notary.stamp(report_path, read_only=True)
        report_rows[role] = {
            "path": str(report_path),
            "sha256": stamped.get("sha256"),
            "bytes": stamped.get("bytes"),
        }
    if defects:
        return None, None, _unique(defects)
    try:
        element_index = _load_json(
            Path(str(control["element_index"])),
            label="plan-alignment element index",
        )
    except ValueError as exc:
        return None, None, (f"SEMANTIC-ELEMENT-INDEX:{exc}",)
    elevations = required_elevations(
        reconstruction_reports={
            role: payloads[role]
            for role in (
                "reconstruction-verification",
                "reconstruction-construction",
            )
        },
        element_index=element_index,
        comparator_report=payloads["comparator"],
        coherence_report=payloads["coherence"],
        atomization_report=payloads["atomization"],
    )
    evidence_payload = {
        "schema_version": SCHEMA_VERSION,
        "node_address": control["node_address"],
        "cell_sha256": control["cell_sha256"],
        "q3_bundle_sha256": control["q3_bundle_sha256"],
        "intent_fingerprint": control["intent_fingerprint"],
        "scope_prefixes": control.get("scope_prefixes") or [],
        "reports": report_rows,
        "required_elevations": elevations,
    }
    evidence_path = Path(control_path).parents[1] / "evidence-index.json"
    path, sha = _freeze_json(evidence_path, evidence_payload)
    return path, sha, ()


def load_current_evidence(path: Path, expected_sha256: str) -> dict:
    stamped = notary.stamp(Path(path))
    if stamped.get("sha256") != expected_sha256:
        raise ValueError(f"semantic evidence index drifted: {path}")
    payload = _load_json(Path(path), label="semantic evidence index")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("semantic evidence index schema_version must equal 1")
    return payload


def elevation_directory(evidence_path: Path) -> Path:
    return Path(evidence_path).parent / "elevations"


def read_elevation_markers(
    *,
    evidence_path: Path,
    evidence_sha256: str,
) -> tuple[dict[str, dict], tuple[str, ...]]:
    """Read valid one-finding/one-question L1 markers without inventing missing rows."""

    try:
        evidence = load_current_evidence(evidence_path, evidence_sha256)
    except ValueError as exc:
        return {}, (f"PLAN-ALIGNMENT-EVIDENCE-DRIFT:{exc}",)
    required = {
        str(row.get("fingerprint")): row
        for row in evidence.get("required_elevations") or []
        if isinstance(row, dict) and row.get("fingerprint")
    }
    root = elevation_directory(evidence_path)
    markers: dict[str, dict] = {}
    defects: list[str] = []
    if not root.is_dir():
        return markers, ()
    for path in sorted(root.glob("*.json")):
        try:
            payload = _load_json(path, label=f"elevation marker {path.name}")
        except ValueError as exc:
            defects.append(f"PLAN-ALIGNMENT-ELEVATION-MARKER:{exc}")
            continue
        expected_fields = {
            "schema_version",
            "finding_fingerprint",
            "semantic_evidence_sha256",
            "proposed_disposition",
            "question",
        }
        if set(payload) != expected_fields or payload.get("schema_version") != SCHEMA_VERSION:
            defects.append(f"PLAN-ALIGNMENT-ELEVATION-MARKER-SCHEMA:{path}")
            continue
        fingerprint = str(payload.get("finding_fingerprint") or "")
        if fingerprint not in required:
            defects.append(f"PLAN-ALIGNMENT-ELEVATION-UNKNOWN-FINDING:{fingerprint}")
            continue
        if path.stem != fingerprint:
            defects.append(
                f"PLAN-ALIGNMENT-ELEVATION-FILENAME:{path.name}:{fingerprint}"
            )
            continue
        if payload.get("semantic_evidence_sha256") != evidence_sha256:
            defects.append(f"PLAN-ALIGNMENT-ELEVATION-EVIDENCE-DRIFT:{fingerprint}")
            continue
        question = str(payload.get("question") or "").strip()
        if not question.endswith("?") or question.count("?") != 1:
            defects.append(
                f"PLAN-ALIGNMENT-ELEVATION-NOT-ONE-CONFIRMABLE-QUESTION:{fingerprint}"
            )
            continue
        disposition = str(payload.get("proposed_disposition") or "").strip()
        if not disposition:
            defects.append(
                f"PLAN-ALIGNMENT-ELEVATION-MISSING-DISPOSITION:{fingerprint}"
            )
            continue
        stamped = notary.stamp(path, read_only=True)
        question_id = f"plan-alignment-{fingerprint[:20]}"
        markers[fingerprint] = {
            "question_id": question_id,
            "finding_fingerprint": fingerprint,
            "finding": required[fingerprint],
            "question": question,
            "proposed_disposition": disposition,
            "semantic_evidence": str(evidence_path),
            "semantic_evidence_sha256": evidence_sha256,
            "cell_sha256": evidence.get("cell_sha256"),
            "intent_fingerprint": evidence.get("intent_fingerprint"),
            "marker": str(path),
            "marker_sha256": stamped.get("sha256"),
        }
    return markers, _unique(defects)


def plan_alignment_pass_blockers(
    l2_binding: Mapping,
    l1_binding: Mapping,
) -> tuple[str, ...]:
    """Return deterministic PASS blockers for the elevate-only human surface."""

    evidence_sha = str(
        l2_binding.get("plan_alignment_semantic_evidence_sha256") or ""
    )
    cell_sha = str(
        l2_binding.get("plan_alignment_semantic_bundle_sha256") or ""
    )
    required = [
        row
        for row in l2_binding.get("plan_alignment_required_elevations") or []
        if isinstance(row, dict) and row.get("fingerprint")
    ]
    questions = l1_binding.get("plan_alignment_owner_questions") or {}
    blockers: list[str] = []
    for finding in required:
        fingerprint = str(finding["fingerprint"])
        matches = [
            row
            for row in questions.values()
            if isinstance(row, dict)
            and row.get("finding_fingerprint") == fingerprint
            and row.get("cell_for") == l2_binding.get("node_address")
        ]
        if len(matches) != 1:
            blockers.append(f"PLAN-ALIGNMENT-ELEVATION-MISSING:{fingerprint}")
            continue
        row = matches[0]
        if (
            row.get("semantic_evidence_sha256") != evidence_sha
            or row.get("cell_sha256") != cell_sha
        ):
            blockers.append(f"PLAN-ALIGNMENT-ELEVATION-DRIFTED:{fingerprint}")
            continue
        status = row.get("status")
        if status == "rejected":
            blockers.append(f"PLAN-ALIGNMENT-ELEVATION-REJECTED:{fingerprint}")
        elif status != "confirmed":
            blockers.append(f"PLAN-ALIGNMENT-ELEVATION-UNANSWERED:{fingerprint}")
    return tuple(blockers)
