"""Review-dispatch artifacts for higher composition gates.

The harness does not choose review findings or gate verdicts. It creates the
pointer packet a review lead reads, then enforces the small deterministic floor
that the lead recorded a review plan and either collected the required check
reports or explicitly used the short-review exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Optional

from harnessd import addressing, ledger, notary, store
from harnessd.spawn import sandbox

_SECTION = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_ID_TOKEN = re.compile(r"\b(?:R|DR)-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*\b")

_TRACE_INDEX_NAME_HINTS = ("rtm", "trace", "coverage")
_GOVERNING_NAME_HINTS = (
    "adr",
    "adrs",
    "decision",
    "decisions",
    "architecture",
    "convention",
    "conventions",
    "substrate",
)
_INTERFACE_NAME_HINTS = (
    "interface",
    "interfaces",
    "contract",
    "contracts",
    "port",
    "ports",
    "schema",
    "schemas",
    "api",
)
_ANCESTOR_GOVERNING_FILES = {"project.md", "design.md", "conventions.md", "architecture.md"}

_REVIEW_CHECK_REPORTS = (
    "reviewers/fidelity-coverage/report.md",
    "reviewers/composition-interface/report.md",
    "reviewers/evidence-credibility/report.md",
    "reviewers/risk-readiness/report.md",
)
CONFIGURABLE_MODULE_REVIEW_AXES = (
    "fidelity-coverage",
    "composition-interface",
    "evidence-credibility",
    "risk-readiness",
    "broad",
)
_BROAD_REVIEW_TASK = (
    "Review the candidate for anything that would make accepting it wrong. "
    "Work without an axis lane. Follow the same evidence, report, attribution, "
    "and sign-off duties as every review-check seat."
)
_L2_REVIEW_CHECK_REPORTS = (
    "reviewers/fidelity-coverage/report.md",
    "reviewers/composition-interface/report.md",
    "reviewers/risk-readiness/report.md",
    "reviewers/user-simulation/report.md",
    "reviewers/performance-robustness/report.md",
)
_HARNESS_ROOT = Path(__file__).resolve().parents[1]
_CHECK_REVIEW_REPORT_TEMPLATE = _HARNESS_ROOT / "operational/shared/templates/check-review-report-template.md"
_REVIEW_ROUTING_VALUES = {"accept-note", "bounce", "escalate"}
_RUBRIC_RELATIVE_CANDIDATES = (
    "acceptance.md",
    "client-brief/intent-spec.md",
    "intent-spec.md",
    "plan/validated-plan-package.md",
    "plan-alignment-verdict.md",
)

_REVIEW_CHECKS = (
    {
        "slug": "fidelity-coverage",
        "label": "Fidelity and Coverage",
        "report": "reviewers/fidelity-coverage/report.md",
        "legacy": {
            "L4+": "brief-coverage",
            "L3+": "area-coverage",
            "L2+": "architecture-coverage",
        },
        "task": {
            "L4+": (
                "Map the L3 workstream brief, inherited requirement IDs, rubric items, and "
                "delegated constraints to named accepted L5 task outputs, accepted deferrals, "
                "or escalations. Flag silent drops, unsanctioned additions, or scope drift."
            ),
            "L3+": (
                "Map the requester/L2 area spec, frozen area design, inherited IDs, ADR "
                "constraints, and area obligations to named accepted L4 workstream outputs, "
                "accepted deferrals, or escalations. Flag silent drops or design drift."
            ),
            "L2+": (
                "Map the frozen intent/spec, architecture, ADRs, and requirement map to named "
                "accepted L3 area outputs, accepted deferrals, or escalations. Flag silent "
                "drops, product-scope drift, or unowned additions."
            ),
        },
        "non_scope": "Do not evaluate seam behavior, evidence trust, code quality, or handoff polish except where they prove a coverage finding.",
    },
    {
        "slug": "composition-interface",
        "label": "Composition and Interface Integrity",
        "report": "reviewers/composition-interface/report.md",
        "legacy": {
            "L4+": "task-interface-fit, workstream-integration, boundary-quality",
            "L3+": "internal-interface-fit, area-integration, exposed-contract",
            "L2+": "cross-area-interface-fit, product-integration, end-to-end-flows",
        },
        "task": {
            "L4+": (
                "Judge whether accepted L5 task outputs form one coherent workstream capability, "
                "including task-to-task contracts, lifecycle assumptions, data/error behavior, "
                "dependency direction, and the workstream boundary exposed to L3. YOUR OWN ORACLE "
                "(the one execution that is yours, 2026-07-17): DRIVE the assembled workstream "
                "artifact through at least one real input-to-output path per exposed capability — "
                "the composed package at the workstream root, which no child ever ran. 'The suite "
                "is green' is a fact you cite from the L5+ gates, never your terminal claim; your "
                "terminal claim is 'the assembled package does X when driven'."
            ),
            "L3+": (
                "Judge whether accepted L4 workstreams form one coherent area/module, including "
                "internal workstream contracts, area integration, and the exposed contract visible "
                "to L2 or sibling areas. YOUR OWN ORACLE (2026-07-17): DRIVE the area's exposed "
                "contract with real calls that cross workstream boundaries — the seam behavior no "
                "workstream gate could exercise. Cite workstream verdicts for everything inside "
                "them; your terminal claim is 'the area's contract does X when driven'."
            ),
            "L2+": (
                "Judge whether accepted L3 areas form one coherent product, including cross-area "
                "contracts, shared flows, product integration, and named end-to-end behavior. "
                "YOUR OWN ORACLE (2026-07-17): RUN real end-to-end user flows on the composed "
                "product — the recipient-visible surface, as a user would drive it. Cite area "
                "verdicts for everything beneath; your terminal claim is 'the product does X when "
                "a user drives it'."
            ),
        },
        "non_scope": "Do not redo coverage bookkeeping or lower local review unless a seam issue depends on that evidence. Do not rerun lower suites — your execution budget is your own oracle above.",
    },
    {
        "slug": "evidence-credibility",
        "label": "Evidence Credibility",
        "report": "reviewers/evidence-credibility/report.md",
        "legacy": {
            "L4+": "lower-review-evidence",
            "L3+": "evidence-quality",
            "L2+": "evidence-quality",
        },
        "task": {
            "L4+": (
                "Judge whether direct L5 children, their L5+ verdicts, deterministic evidence, "
                "and report pointers are present, current, specific, and credible enough for L4+ "
                "to rely on by pointer."
            ),
            "L3+": (
                "Judge whether direct L4 workstream children, their L4+ verdicts, integration "
                "evidence, and report pointers are present, current, specific, and credible enough "
                "for L3+ to rely on by pointer."
            ),
            "L2+": (
                "Judge whether direct L3 area children, their L3+ verdicts, product evidence, and "
                "report pointers are present, current, specific, and credible enough for L2+ to "
                "rely on by pointer."
            ),
        },
        "non_scope": "Do not rerun lower suites as a generic confidence ritual. Use bounded probes only when evidence is missing, contradictory, stale, vague, or suspicious, and state the reason.",
        "required_accounting": (
            "Include a Lower Evidence Accounting table: lower child or output, verdict pointer, "
            "currency/stamp checked, credible enough to rely on, and rationale."
        ),
    },
    {
        "slug": "risk-readiness",
        "label": "Risk, Substrate, and Handoff Readiness",
        "report": "reviewers/risk-readiness/report.md",
        "legacy": {
            "L4+": "parent-consumability",
            "L3+": "risk-and-deviation",
            "L2+": "shared-ownership-and-substrate, requester-handoff",
        },
        "task": {
            "L4+": (
                "Judge residual workstream risks, named deviations, operational/substrate concerns "
                "at workstream altitude, and whether the package is clear enough for L3 to consume "
                "without reconstructing the workstream."
            ),
            "L3+": (
                "Judge area risks, deviations, substrate or ownership concerns at area altitude, "
                "and whether the package is clear enough for L2/requester consumption."
            ),
            "L2+": (
                "Judge product-level residual risks, shared substrate and ownership coherence, "
                "requester handoff quality, and whether L1/user can evaluate the delivered product "
                "without reconstructing lower gates."
            ),
        },
        "non_scope": "Do not relitigate lower implementation choices unless they create altitude-level risk or handoff failure.",
        "required_accounting": (
            "Include a Risk Trigger Scan table. Cover security/privacy, data/state, "
            "migration/compatibility, performance/scale, operations/observability, "
            "domain/policy, substrate/ownership, and handoff/readiness. Mark each trigger "
            "applies or N/A because, with an evidence pointer or rationale."
        ),
        "required_accounting_by_level": {
            "L2+": (
                "Also include the exact three-item Security Basics checklist: "
                "secrets/credentials are absent from delivered artifacts and probe evidence; "
                "no accidental network listener or exposure exists beyond the documented face; "
                "trust-boundary inputs have basic sanity validation."
            ),
        },
    },
    {
        "slug": "broad",
        "label": "Broad Acceptance Review",
        "report": "reviewers/broad/report.md",
        "legacy": {
            "L4+": "unscoped acceptance review",
            "L3+": "unscoped acceptance review",
            "L2+": "unscoped acceptance review",
        },
        "task": {
            "L4+": _BROAD_REVIEW_TASK,
            "L3+": _BROAD_REVIEW_TASK,
            "L2+": _BROAD_REVIEW_TASK,
        },
        "non_scope": (
            "Do not mutate the candidate, author sibling reports, write the final gate artifact, "
            "or exercise verdict authority."
        ),
        "unscoped": True,
    },
    {
        "slug": "user-simulation",
        "label": "User-Simulation Product Probe",
        "report": "reviewers/user-simulation/report.md",
        "legacy": {"L2+": "recipient-visible journeys, negative-and-MNF journeys"},
        "task": {
            "L2+": (
                "Drive every confirmed intent-spec journey, including negative and MNF journeys, "
                "against the real assembled artifact through its documented face on the assigned "
                "manifest-verified disposable instance. Use only the harness-generated probe "
                "roster and artifact-declared invocation authority."
            ),
        },
        "non_scope": (
            "Do not invent journeys, commands, usability requirements, or accessibility criteria. "
            "Do not inspect or mutate the live producer or a sibling probe instance."
        ),
        "required_accounting": (
            "Include one Journey Accounting row per roster journey/MNF journey with exact "
            "invocation, observation, frozen anchor, disposition, and evidence pointer."
        ),
        "probe_instruction": "user-simulation",
    },
    {
        "slug": "performance-robustness",
        "label": "Performance and Robustness Product Probe",
        "report": "reviewers/performance-robustness/report.md",
        "legacy": {"L2+": "performance, failure-under-load, restart-and-boundary behavior"},
        "task": {
            "L2+": (
                "Probe specified thresholds and MNF failure paths under load, concurrency, "
                "interruption, restart, and boundary conditions through the documented artifact "
                "face on the assigned manifest-verified disposable instance. When no quantitative "
                "threshold exists, measurements are non-blocking inventory."
            ),
        },
        "non_scope": (
            "Do not invent thresholds, generic production-readiness requirements, commands, "
            "benchmark corpora, or accessibility criteria. Do not inspect or mutate the live "
            "producer or a sibling probe instance."
        ),
        "required_accounting": (
            "Separate Specified Threshold Accounting, MNF/Robustness Accounting, and explicitly "
            "non-blocking Unthresholded Sanity Inventory."
        ),
        "probe_instruction": "performance-robustness",
    },
)

_HIGHER_CHECK_REPORTS = {
    "L4+": _REVIEW_CHECK_REPORTS,
    "L4": _REVIEW_CHECK_REPORTS,
    "L3+": _REVIEW_CHECK_REPORTS,
    "L3": _REVIEW_CHECK_REPORTS,
    "L2+": _L2_REVIEW_CHECK_REPORTS,
    "L2": _L2_REVIEW_CHECK_REPORTS,
}

_GATE_ARTIFACT_BY_REVIEW_LEVEL = {
    "L4+": "gate-composition-report.md",
    "L4": "gate-composition-report.md",
    "L3+": "gate-area-composition-review.md",
    "L3": "gate-area-composition-review.md",
    "L2+": "gate-composition-review.md",
    "L2": "gate-composition-review.md",
}
_GATE_VERDICT = re.compile(r"\bVERDICT:\s*(ACCEPT|BOUNCE|ESCALATE)\b", re.IGNORECASE)
_EXPECTED_CHILD_LEVEL_BY_PRODUCER_LEVEL = {
    "L2": "L3",
    "L3": "L4",
    "L4": "L5",
}
_PLANNING_PURPOSE = "planning"
_NON_IMPLEMENTATION_CHILD_PURPOSES = {_PLANNING_PURPOSE, "test_author"}
_CANDIDATE_ARTIFACT_MANIFEST = "candidate-artifacts.json"
_CANDIDATE_ARTIFACT_SNAPSHOT_DIR = "candidate-snapshot"
_CANDIDATE_EXCLUDED_DIRS = {
    addressing.MESSAGES_DIRNAME,
    sandbox.RUNTIME_SCRATCH_DIRNAME,
    ".git",
    ".harness-outbox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "reviews",
    "scratch",
    "venv",
}
_CANDIDATE_EXCLUDED_DIR_SUFFIXES = (".egg-info",)
_CANDIDATE_EXCLUDED_FILES = {".DS_Store"}
_CANDIDATE_MUTABLE_PROCESS_FILES = {"log.md", "plan.md"}


def is_higher_review_gate(binding: Optional[dict]) -> bool:
    return bool(
        binding
        and binding.get("gate_for")
        and (binding.get("level") or "").strip() in _HIGHER_CHECK_REPORTS
    )


def configured_module_panel_axes(binding: Optional[dict]) -> Optional[tuple[str, ...]]:
    """Return the first matching commissioned L3 module panel, else ``None``."""
    if not isinstance(binding, dict) or ledger.RUNTIME_ROOT is None:
        return None
    level = (binding.get("level") or "").strip()
    producer_address = str(binding.get("gate_for") or "").strip()
    if level not in {"L3", "L3+"} or not producer_address:
        return None
    live_map = ledger.all_nodes()
    producer = live_map.get(producer_address) or {}
    if (producer.get("level") or "").strip() != "L3":
        return None
    roots = sorted(
        (
            candidate
            for candidate in live_map.values()
            if candidate.get("parent_address") is None
            and isinstance(candidate.get("review_panel_arms"), list)
        ),
        key=lambda candidate: str(candidate.get("node_address") or ""),
    )
    for root in roots:
        for arm in root.get("review_panel_arms") or []:
            pattern = str((arm or {}).get("pattern") or "")
            if fnmatchcase(producer_address, pattern):
                return tuple(str(axis) for axis in (arm or {}).get("axes") or [])
    return None


def _review_check_spec(raw: dict, canonical: str) -> dict:
    spec = dict(raw)
    spec["task"] = raw["task"].get(canonical) or raw["task"].get("L4+") or ""
    spec["legacy"] = raw["legacy"].get(canonical, "")
    by_level = raw.get("required_accounting_by_level") or {}
    if canonical in by_level:
        spec["required_accounting"] = " ".join(
            value
            for value in (
                str(raw.get("required_accounting") or "").strip(),
                str(by_level[canonical]).strip(),
            )
            if value
        )
    return spec


def required_check_report_names(level_or_binding) -> tuple[str, ...]:
    """Return exact FULL-mode check report basenames for a higher review level."""
    if isinstance(level_or_binding, dict):
        level = (level_or_binding.get("level") or "").strip()
        configured = configured_module_panel_axes(level_or_binding)
        if configured is not None:
            by_slug = {str(raw["slug"]): raw for raw in _REVIEW_CHECKS}
            return tuple(str(by_slug[axis]["report"]) for axis in configured)
    else:
        level = (level_or_binding or "").strip()
    return _HIGHER_CHECK_REPORTS.get(level, ())


def required_review_check_specs(level_or_binding) -> tuple[dict, ...]:
    """Return the exact level-specific FULL-mode review-check specs."""
    if isinstance(level_or_binding, dict):
        level = (level_or_binding.get("level") or "").strip()
    else:
        level = (level_or_binding or "").strip()
    if level not in _HIGHER_CHECK_REPORTS:
        return ()
    canonical = _canonical_review_level(level)
    configured = (
        configured_module_panel_axes(level_or_binding)
        if isinstance(level_or_binding, dict)
        else None
    )
    if configured is not None:
        by_slug = {str(raw["slug"]): raw for raw in _REVIEW_CHECKS}
        return tuple(_review_check_spec(by_slug[axis], canonical) for axis in configured)
    required_reports = set(_HIGHER_CHECK_REPORTS[level])
    specs: list[dict] = []
    for raw in _REVIEW_CHECKS:
        if str(raw["report"]) not in required_reports:
            continue
        specs.append(_review_check_spec(raw, canonical))
    return tuple(specs)


def is_review_check_binding(binding: Optional[dict]) -> bool:
    return bool(binding and binding.get("review_check_for"))


def report_has_routing_field(text: str) -> bool:
    return _has_routing_field(text)


def review_mode_for_gate(review_address: str, binding: dict) -> Optional[str]:
    """Return FULL/SHORT from a gate lead's review-plan.md, if it is available."""
    gate_dir = _resolve_gate_dir(review_address, binding)
    if gate_dir is None:
        return None
    plan = gate_dir / "review-plan.md"
    text = _read(plan).strip() if plan.is_file() else ""
    return _review_mode(text) if text else None


def review_checks_ready_for_dispatch(review_address: str, binding: dict) -> bool:
    """Return true once a higher review lead has recorded a dispatchable FULL plan."""
    if not is_higher_review_gate(binding):
        return False
    gate_dir = _resolve_gate_dir(review_address, binding)
    if gate_dir is None:
        return False
    packet = gate_dir / "review-packet.md"
    if not packet.is_file() or not _read(packet).strip():
        return False
    plan = gate_dir / "review-plan.md"
    plan_text = _read(plan).strip() if plan.is_file() else ""
    if not plan_text or _review_mode(plan_text) != "FULL":
        return False
    role_selection = _section_text(plan_text, "Role Selection")
    if not _meaningful_section(role_selection):
        return False
    return _full_role_selection_defect(review_address, binding, role_selection) is None


def review_check_dispatch_blocker(review_address: str, binding: dict) -> Optional[dict]:
    """Return a plan defect that blocks FULL reviewer dispatch, if the lead can repair it."""
    if not is_higher_review_gate(binding):
        return None
    gate_dir = _resolve_gate_dir(review_address, binding)
    if gate_dir is None:
        return None
    packet = gate_dir / "review-packet.md"
    if not packet.is_file() or not _read(packet).strip():
        return None
    plan = gate_dir / "review-plan.md"
    plan_text = _read(plan).strip() if plan.is_file() else ""
    if not plan_text:
        return None
    plan_sha256 = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
    mode = _review_mode(plan_text)
    if mode is None:
        defect = (
            f"MISSING-REVIEW-MODE: {review_address} review-plan.md must contain exactly one "
            "plain metadata line: `Review Mode: FULL` or `Review Mode: SHORT`"
        )
    elif mode != "FULL":
        return None
    else:
        role_selection = _section_text(plan_text, "Role Selection")
        if not _meaningful_section(role_selection):
            defect = (
                f"MISSING-ROLE-SELECTION: {review_address} review-plan.md must contain a "
                "non-empty `## Role Selection` section naming selected review checks and why "
                "they are sufficient"
            )
        else:
            defect = _full_role_selection_defect(review_address, binding, role_selection)
            if not defect:
                return None
    return {
        "defect": defect,
        "plan_sha256": plan_sha256,
        "plan_path": str(plan),
        "gate_dir": str(gate_dir),
    }


def review_check_report_path(gate_dir: Path, spec: dict) -> Path:
    return gate_dir / str(spec["report"])


def render_review_check_brief(
    *,
    review_address: str,
    review_binding: dict,
    producer_address: str,
    gate_id: str,
    gate_dir: Path,
    spec: dict,
) -> str:
    """Render the bounded task brief for one auxiliary review-check seat."""
    report = review_check_report_path(gate_dir, spec)
    packet = gate_dir / "review-packet.md"
    canonical = _canonical_review_level((review_binding.get("level") or "").strip())
    probe_lines: list[str] = []
    if spec.get("probe_instruction"):
        probe_lines = [
            f"- Product-probe roster: `{spec.get('probe_roster')}`",
            f"- Product-probe roster sha256: `{spec.get('probe_roster_sha256')}`",
            f"- Disposable instance: `{spec.get('probe_instance_root')}`",
            f"- Disposable-instance manifest: `{spec.get('probe_instance_manifest')}`",
            "- Run only inside the disposable instance. The live producer workspace and sibling "
            "probe are outside your physical read policy.",
        ]
    method_scope = (
        "- Review across the candidate without an axis lane; pursue any evidence-backed reason "
        "that accepting it would be wrong."
        if spec.get("unscoped")
        else "- Stay inside this check's scope. Neighboring checks are owned by other reviewers."
    )
    return "\n".join(
        [
            f"# Review Check Brief — {spec['label']}",
            "",
            f"- Gate lead: `{review_address}`",
            f"- Candidate: `{producer_address}`",
            f"- Gate id: `{gate_id}`",
            f"- Review packet: `{packet}`",
            f"- Assigned report path: `{report}`",
            f"- Gate altitude: `{canonical}`",
            f"- Legacy coverage folded into this check: `{spec.get('legacy') or 'none'}`",
            *probe_lines,
            "",
            "## Task",
            "",
            str(spec["task"]),
            "",
            "## Method",
            "",
            "- Start from the review packet and the pointers it names.",
            method_scope,
            "- Treat lower gates as competent by default. Judge whether their evidence is present, "
            "current, specific, and appropriate for your check.",
            "- First try to answer this check from the lower verdicts, reports, manifests, and "
            "candidate pointers. Run a bounded probe only when a named uncertainty remains and the "
            "probe answers this check's own altitude question.",
            "- If you run or repeat a lower-level command, state the unresolved question it answered "
            "in your report.",
            "- Produce findings only. Do not change the candidate, do not write the final gate "
            "artifact, and do not sign ACCEPT/BOUNCE/ESCALATE for the candidate.",
            "",
            "## Out Of Scope",
            "",
            str(spec["non_scope"]),
            "",
            "## Output Contract",
            "",
            f"Write exactly one check report at `{report}`.",
            f"Use the shared check report shape at `{_CHECK_REVIEW_REPORT_TEMPLATE}`.",
            "",
            "- include a plain `Recommended Routing: accept-note`, `Recommended Routing: bounce`, "
            "or `Recommended Routing: escalate` line near the top;",
            "- include scope read, findings, evidence pointers, severity, confidence, criterion or "
            "contract, and notes for the gate lead;",
            *(["- " + str(spec["required_accounting"])] if spec.get("required_accounting") else []),
            "- do not write the final `gate-*` artifact.",
            "",
        ]
    )


def review_check_defects(review_address: str, binding: dict, gate_dir: Path) -> list[str]:
    """Return defects when FULL-mode reports lack matching completed review-check seats."""
    defects: list[str] = []
    producer_address = (binding or {}).get("gate_for")
    producer = ledger.read_binding(producer_address) if producer_address else None
    gate_id = str((producer or {}).get("gate_id") or "").strip()
    if not gate_id:
        return defects
    for spec in required_review_check_specs(binding):
        report = review_check_report_path(gate_dir, spec)
        check = _matching_review_check_binding(
            review_address=review_address,
            gate_id=gate_id,
            report=report,
            slug=str(spec["slug"]),
        )
        if check is None:
            defects.append(
                f"MISSING-REVIEW-CHECK-SEAT: {review_address} full review mode requires a "
                f"completed reviewer seat for {spec['label']} writing {report}"
            )
            continue
        if check.get("state") != "done":
            defects.append(
                f"INCOMPLETE-REVIEW-CHECK-SEAT: {check.get('node_address')} for "
                f"{review_address} is {check.get('state')!r}, not done"
            )
    return defects


def gate_id_for(producer_address: str, review_address: str, signal_artifact_identity: Optional[str]) -> str:
    seed = "\n".join([
        producer_address or "",
        review_address or "",
        signal_artifact_identity or "manual",
    ])
    return "gate-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def create_review_packet(
    producer_address: str,
    producer_binding: dict,
    review_address: str,
    signal_artifact_identity: Optional[str],
) -> dict:
    """Create the harness-owned review packet and return binding fields.

    Best-effort callers should catch exceptions; this function is deterministic
    but still depends on the runtime tree being bound and writable.
    """
    if ledger.RUNTIME_ROOT is None:
        return {}
    node_dir = addressing.node_dir(producer_address, ledger.RUNTIME_ROOT)
    gate_id = gate_id_for(producer_address, review_address, signal_artifact_identity)
    gate_dir = node_dir / "reviews" / gate_id
    manifest_fields = _write_candidate_artifact_manifest(
        producer_address,
        gate_id,
        gate_dir,
        node_dir,
    )
    packet = gate_dir / "review-packet.md"
    if not packet.exists():
        text = _render_review_packet(
            producer_address,
            producer_binding,
            review_address,
            signal_artifact_identity,
            gate_id,
            gate_dir,
            node_dir,
            manifest_fields,
        )
        store.atomic_replace(packet, lambda handle: handle.write(text))
    return {
        "gate_id": gate_id,
        "gate_review_dir": str(gate_dir),
        "gate_review_packet": str(packet),
        **manifest_fields,
    }


def candidate_artifact_manifest_defects_for_review(review_binding: dict) -> list[str]:
    """Return defects if the producer's current artifacts no longer match the submitted manifest.

    Missing manifest fields mean this is an older hand-seeded candidate and are tolerated for
    backward compatibility. Once a candidate was submitted through ``create_review_packet``, the
    manifest path/hash on the producer binding becomes the candidate identity for review routing.
    """
    producer_address = (review_binding or {}).get("gate_for")
    if not producer_address:
        return []
    producer = ledger.read_binding(producer_address) or {}
    manifest_path_raw = str(producer.get("gate_candidate_artifact_manifest") or "").strip()
    if not manifest_path_raw:
        return []
    manifest_path = Path(manifest_path_raw)
    expected_manifest_sha = str(producer.get("gate_candidate_artifact_manifest_sha256") or "").strip()
    manifest_stamp = notary.stamp(manifest_path)
    if manifest_stamp.get("present") is not True:
        return [
            f"CANDIDATE-ARTIFACT-MANIFEST-MISSING: current candidate {producer_address} "
            f"records {manifest_path}, but the manifest cannot be read"
        ]
    raw = manifest_path.read_bytes()
    if expected_manifest_sha and not notary.check(
        {"sha256": expected_manifest_sha},
        target=manifest_path,
    ):
        return [
            f"CANDIDATE-ARTIFACT-MANIFEST-DRIFT: current candidate {producer_address} "
            f"records manifest sha {expected_manifest_sha}, but {manifest_path} is now "
            f"{manifest_stamp.get('sha256')}"
        ]
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [
            f"CANDIDATE-ARTIFACT-MANIFEST-MALFORMED: current candidate {producer_address} "
            f"manifest {manifest_path} is not valid JSON"
        ]
    if ledger.RUNTIME_ROOT is None:
        return []
    node_dir = addressing.node_dir(producer_address, ledger.RUNTIME_ROOT)
    current_paths = _candidate_artifact_paths(node_dir)
    expected_collection = {
        "present": True,
        "files": {
            str(entry.get("path")): {
                "present": True,
                "sha256": entry.get("sha256"),
                "bytes": entry.get("bytes"),
            }
            for entry in (manifest.get("artifacts") or [])
            if isinstance(entry, dict) and entry.get("path")
        },
    }
    collection_check = notary.check(
        expected_collection,
        target=node_dir,
        members=current_paths,
        root_label=str(node_dir),
    )
    expected_entries = {
        str(entry.get("path")): entry
        for entry in (manifest.get("artifacts") or [])
        if isinstance(entry, dict) and entry.get("path")
    }
    current_entries = collection_check.current.get("files") or {}
    defects: list[str] = []
    for relpath, expected in sorted(expected_entries.items()):
        snapshot_path_raw = str(expected.get("snapshot_path") or "").strip()
        if not snapshot_path_raw:
            continue
        snapshot_path = Path(snapshot_path_raw)
        snapshot_check = notary.check(
            {
                "present": True,
                "sha256": expected.get("sha256"),
                "bytes": expected.get("bytes"),
            },
            target=snapshot_path,
        )
        if snapshot_check.current.get("present") is not True:
            defects.append(
                f"CANDIDATE-ARTIFACT-SNAPSHOT-MISSING: {producer_address} artifact {relpath} "
                f"records snapshot {snapshot_path}, but the snapshot cannot be read"
            )
            continue
        if not snapshot_check:
            snapshot_stamp = snapshot_check.current
            defects.append(
                f"CANDIDATE-ARTIFACT-SNAPSHOT-DRIFT: {producer_address} artifact {relpath} "
                f"snapshot {snapshot_path} no longer matches the submitted manifest "
                f"(expected sha={expected.get('sha256')} bytes={expected.get('bytes')}; "
                f"snapshot sha={snapshot_stamp.get('sha256')} "
                f"bytes={snapshot_stamp.get('bytes')})"
            )
    mismatch_pairs = {(item.get("kind"), item.get("path")) for item in collection_check.mismatches}
    for relpath in sorted(
        path for kind, path in mismatch_pairs if kind == "removed" and path
    ):
        defects.append(
            f"CANDIDATE-ARTIFACT-REMOVED: {producer_address} artifact {relpath} was present "
            "when the candidate was submitted but is now missing"
        )
    for relpath in sorted(
        path for kind, path in mismatch_pairs if kind == "added" and path
    ):
        defects.append(
            f"CANDIDATE-ARTIFACT-ADDED: {producer_address} artifact {relpath} was created "
            "after candidate submission and was not part of the reviewed manifest"
        )
    changed_paths = {
        path for kind, path in mismatch_pairs
        if kind in {"sha256", "bytes"} and path in expected_entries
    }
    for relpath in sorted(changed_paths):
        expected = expected_entries[relpath]
        current_entry = current_entries[relpath]
        defects.append(
            f"CANDIDATE-ARTIFACT-DRIFT: {producer_address} artifact {relpath} changed "
            f"after candidate submission (expected sha={expected.get('sha256')} "
            f"bytes={expected.get('bytes')}; current sha={current_entry.get('sha256')} "
            f"bytes={current_entry.get('bytes')})"
        )
    return defects[:20]


def _write_candidate_artifact_manifest(
    producer_address: str,
    gate_id: str,
    gate_dir: Path,
    node_dir: Path,
) -> dict:
    manifest_path = gate_dir / _CANDIDATE_ARTIFACT_MANIFEST
    snapshot_dir = gate_dir / _CANDIDATE_ARTIFACT_SNAPSHOT_DIR
    if not manifest_path.exists():
        payload = _candidate_artifact_manifest_payload(
            producer_address,
            gate_id,
            node_dir,
            snapshot_dir=snapshot_dir,
        )
        data = _canonical_manifest_text(payload)
        store.atomic_replace(manifest_path, lambda handle: handle.write(data))
    manifest_stamp = notary.stamp(manifest_path)
    return {
        "gate_candidate_artifact_manifest": str(manifest_path),
        "gate_candidate_artifact_manifest_sha256": manifest_stamp.get("sha256"),
        "gate_candidate_artifact_snapshot_dir": str(snapshot_dir),
    }


def _candidate_artifact_manifest_payload(
    producer_address: str,
    gate_id: str,
    node_dir: Path,
    *,
    snapshot_dir: Optional[Path] = None,
) -> dict:
    paths = _candidate_artifact_paths(node_dir)
    stamped = notary.stamp(
        node_dir,
        members=paths,
        root_label=str(node_dir),
        snapshot_to=snapshot_dir,
    )
    artifacts = []
    for relpath, file_stamp in stamped.get("files", {}).items():
        if file_stamp.get("present") is not True:
            raise FileNotFoundError(node_dir / relpath)
        path = node_dir / relpath
        entry = {
            "path": relpath,
            "source_path": str(path),
            "bytes": file_stamp.get("bytes"),
            "sha256": file_stamp.get("sha256"),
        }
        if snapshot_dir is not None:
            snapshot_path = snapshot_dir / relpath
            entry["snapshot_relpath"] = (
                Path(_CANDIDATE_ARTIFACT_SNAPSHOT_DIR) / relpath
            ).as_posix()
            entry["snapshot_path"] = str(snapshot_path)
        artifacts.append(entry)
    return {
        "schema_version": 2 if snapshot_dir is not None else 1,
        "candidate": producer_address,
        "gate_id": gate_id,
        "root": str(node_dir),
        "snapshot_root": str(snapshot_dir) if snapshot_dir is not None else None,
        "artifacts": artifacts,
    }


def _canonical_manifest_text(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _candidate_artifact_paths(node_dir: Path) -> list[Path]:
    paths: list[Path] = []
    try:
        iterator = node_dir.rglob("*")
        for path in iterator:
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(node_dir)
            except ValueError:
                continue
            if _candidate_artifact_excluded(rel):
                continue
            paths.append(path)
    except OSError:
        return []
    return sorted(paths, key=lambda p: p.relative_to(node_dir).as_posix())


def _candidate_artifact_excluded(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return True
    if any(part in _CANDIDATE_EXCLUDED_DIRS for part in parts[:-1]):
        return True
    if parts[0] in _CANDIDATE_EXCLUDED_DIRS:
        return True
    if any(part.endswith(_CANDIDATE_EXCLUDED_DIR_SUFFIXES) for part in parts[:-1]):
        return True
    name = parts[-1]
    if name in _CANDIDATE_EXCLUDED_FILES:
        return True
    if name in _CANDIDATE_MUTABLE_PROCESS_FILES:
        return True
    # The pass-8 renderer removed the daemon aggregate write path, but historical/runtime replay
    # may still contain `log.aggregate.md` / `status.aggregate.md`. They remain excluded from
    # frozen candidate identity so the r6 CANDIDATE-ARTIFACT-DRIFT livelock cannot recur.
    if name.endswith(".aggregate.md"):
        return True
    if name.startswith("."):
        return True
    return False


def dispatch_contract_defects(review_address: str, binding: dict) -> list[str]:
    """Return deterministic review-dispatch defects for a higher review gate."""
    if not is_higher_review_gate(binding):
        return []
    gate_dir = _resolve_gate_dir(review_address, binding)
    if gate_dir is None:
        return [
            f"MISSING-REVIEW-PACKET: {review_address} is a higher review gate but no "
            "reviews/<gate-id>/review-packet.md or review-plan.md could be resolved"
        ]
    packet = gate_dir / "review-packet.md"
    if not packet.is_file() or not _read(packet).strip():
        return [
            f"MISSING-REVIEW-PACKET: {review_address} signed DONE without the harness-owned "
            f"{packet} packet that grounds the gate candidate"
        ]
    plan = gate_dir / "review-plan.md"
    if not plan.is_file() or not (plan_text := _read(plan).strip()):
        return [
            f"MISSING-REVIEW-PLAN: {review_address} signed DONE without {plan} — the gate "
            "lead must record role selection before rendering a verdict"
        ]
    mode = _review_mode(plan_text)
    if mode is None:
        return [
            f"MISSING-REVIEW-MODE: {review_address} review-plan.md must contain exactly one "
            "plain metadata line: `Review Mode: FULL` or `Review Mode: SHORT`"
        ]
    role_selection = _section_text(plan_text, "Role Selection")
    if not _meaningful_section(role_selection):
        return [
            f"MISSING-ROLE-SELECTION: {review_address} review-plan.md must contain a non-empty "
            "`## Role Selection` section naming selected review checks and why they are sufficient"
        ]
    if mode == "SHORT":
        defect = _short_exception_defect(review_address, plan_text)
        if defect:
            return [defect]
        lower_defect = _accepted_lower_execution_defect(review_address, binding, gate_dir)
        return [lower_defect] if lower_defect else []

    role_selection_defect = _full_role_selection_defect(review_address, binding, role_selection)
    if role_selection_defect:
        return [role_selection_defect]

    defects: list[str] = []
    for name in required_check_report_names(binding):
        report = gate_dir / name
        text = _read(report).strip() if report.is_file() else ""
        if not text:
            defects.append(
                f"MISSING-REVIEWER-REPORT: {review_address} full review mode requires "
                f"{report}"
            )
        elif not _has_routing_field(text):
            defects.append(
                f"MISSING-REVIEWER-ROUTING: {report} must include a plain line "
                "`Recommended Routing: <accept-note|bounce|escalate>` "
                "so the orchestrator synthesis is auditable"
            )
    defects.extend(review_check_defects(review_address, binding, gate_dir))
    lower_defect = _accepted_lower_execution_defect(review_address, binding, gate_dir)
    if lower_defect:
        defects.append(lower_defect)
    return defects


def _review_mode(plan_text: str) -> Optional[str]:
    matches = []
    for line in _normalized_lines(plan_text):
        if not line.lower().startswith("review mode:"):
            continue
        value = line.split(":", 1)[1].strip().upper()
        if value in {"FULL", "SHORT"}:
            matches.append(value)
    return matches[0] if len(matches) == 1 else None


def _short_exception_defect(review_address: str, plan_text: str) -> Optional[str]:
    has_marker = False
    for line in _normalized_lines(plan_text):
        if not line.lower().startswith("short review exception:"):
            continue
        value = line.split(":", 1)[1].strip().upper()
        has_marker = value == "YES"
        break
    if not has_marker:
        return (
            f"MISSING-SHORT-REVIEW-EXCEPTION: {review_address} used short review mode "
            "without `Short Review Exception: YES` in review-plan.md"
        )
    section = _section_text(plan_text, "Short Review Exception")
    rows = _table_rows(section)
    if len(rows) < 6:
        return (
            f"INCOMPLETE-SHORT-REVIEW-EVIDENCE: {review_address} used short review mode "
            "without evidence rows for every short-exception condition"
        )
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if (
            len(cells) < 4
            or cells[1].strip().upper() != "YES"
            or not _meaningful_value(cells[2])
            or not _meaningful_value(cells[3])
        ):
            return (
                f"INCOMPLETE-SHORT-REVIEW-EVIDENCE: {review_address} short review rows must "
                "include condition, explicit YES, evidence pointer, and rationale"
            )
    return None


def _has_routing_field(text: str) -> bool:
    values = []
    for line in _normalized_lines(text):
        lowered = line.lower()
        if lowered.startswith("routing:"):
            return False
        if lowered.startswith("recommended routing:"):
            values.append(line.split(":", 1)[1].strip().lower())
    return len(values) == 1 and values[0] in _REVIEW_ROUTING_VALUES


def _full_role_selection_defect(review_address: str, binding: dict, role_selection: str) -> Optional[str]:
    text = (role_selection or "").lower()
    missing = []
    for spec in required_review_check_specs(binding):
        slug = str(spec.get("slug") or "")
        report = str(spec.get("report") or "")
        if slug and (slug in text or report.lower() in text):
            continue
        missing.append(slug)
    if missing:
        canonical = _canonical_review_level((binding.get("level") or "").strip())
        configured = configured_module_panel_axes(binding)
        if configured is not None:
            roster_label = "the configured module review-check panel"
        else:
            roster_label = (
                "all five L2+ product-altitude review-check axes"
                if canonical == "L2+"
                else "all four V1 review-check axes"
            )
        return (
            f"INCOMPLETE-ROLE-SELECTION: {review_address} FULL review mode must name "
            f"{roster_label} by slug or report path; missing "
            + ", ".join(sorted(missing))
        )
    return None


def _canonical_review_level(level: str) -> str:
    if level in {"L4", "L4+"}:
        return "L4+"
    if level in {"L3", "L3+"}:
        return "L3+"
    if level in {"L2", "L2+"}:
        return "L2+"
    return level


def _matching_review_check_binding(
    *,
    review_address: str,
    gate_id: str,
    report: Path,
    slug: str,
) -> Optional[dict]:
    expected_report = str(report)
    for _address, check in ledger.all_nodes().items():
        if not isinstance(check, dict):
            continue
        if check.get("review_check_for") != review_address:
            continue
        if gate_id and str(check.get("gate_id") or "") != gate_id:
            continue
        if str(check.get("review_check_axis") or "") != slug:
            continue
        if str(check.get("review_check_report") or "") != expected_report:
            continue
        return check
    return None


def _normalized_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        line = line.replace("**", "").replace("`", "").strip()
        if line:
            lines.append(line)
    return lines


def _section_text(text: str, title: str) -> str:
    matches = list(_SECTION.finditer(text or ""))
    wanted = title.strip().lower()
    for idx, match in enumerate(matches):
        heading = match.group(1).strip().lower()
        if not _heading_matches(heading, wanted):
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text or "")
        return (text or "")[start:end]
    return ""


def _heading_matches(heading: str, wanted: str) -> bool:
    if heading == wanted:
        return True
    suffix = heading[len(wanted):] if heading.startswith(wanted) else ""
    return suffix.startswith((" —", " –", " -", ":", " ("))


def _meaningful_section(text: str) -> bool:
    return any(_meaningful_value(line) for line in _normalized_lines(text))


def _meaningful_value(value: str) -> bool:
    stripped = (value or "").strip()
    if not stripped:
        return False
    if stripped.startswith("<") or stripped.endswith(">"):
        return False
    return True


def _table_rows(section: str) -> list[str]:
    rows: list[str] = []
    for raw in (section or "").splitlines():
        line = raw.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip().lower() for c in line.strip("|").split("|")]
        if cells and cells[0] in {"condition", "review check"}:
            continue
        if "<short exception condition>" in line:
            continue
        rows.append(line)
    return rows


def _resolve_gate_dir(review_address: str, binding: dict) -> Optional[Path]:
    producer_address = binding.get("gate_for")
    if producer_address:
        producer = ledger.read_binding(producer_address) or {}
        path = producer.get("gate_review_dir")
        if path:
            return Path(path)
    if ledger.RUNTIME_ROOT is None:
        return None
    root = addressing.node_dir(review_address, ledger.RUNTIME_ROOT) / "reviews"
    try:
        candidates = sorted(
            p for p in root.iterdir()
            if p.is_dir() and ((p / "review-packet.md").exists() or (p / "review-plan.md").exists())
        )
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _render_review_packet(
    producer_address: str,
    producer_binding: dict,
    review_address: str,
    signal_artifact_identity: Optional[str],
    gate_id: str,
    gate_dir: Path,
    node_dir: Path,
    manifest_fields: Optional[dict] = None,
) -> str:
    files = _top_level_markdown_pointers(node_dir)
    lower = _lower_gate_pointers(node_dir)
    expected_lower = _expected_lower_execution_evidence_lines(producer_address, producer_binding)
    trace = _trace_coverage_pointers(node_dir)
    governing = _governing_decision_pointers(node_dir)
    interfaces = _interface_contract_pointers(node_dir)
    verification = _verification_runtime_pointers(node_dir)
    rubric_pointers = _acceptance_rubric_pointers(node_dir)
    review_binding = ledger.read_binding(review_address) or {}
    check_reports = required_check_report_names(review_binding) or required_check_report_names(
        producer_binding
    )
    configured_axes = configured_module_panel_axes(review_binding)
    if check_reports:
        check_count = len(check_reports)
        review_instruction_lines = [
            "- Write `review-plan.md` in this review directory before review-check seats can run.",
            "- In this harness runtime, `Review Mode: FULL` in `review-plan.md` is the dispatch request: "
            "the daemon opens independent review-check seats from the plan.",
            *(
                [
                    "- This module's commissioned panel supersedes the static four-axis default: "
                    + ", ".join(configured_axes)
                    + "."
                ]
                if configured_axes is not None
                else []
            ),
            "- In `review-plan.md`, include exactly one plain line: `Review Mode: FULL` or `Review Mode: SHORT`.",
            "- In `review-plan.md`, include a non-empty `## Role Selection` section before the verdict artifact.",
            f"- Use FULL for normal L4+/L3+/L2+ gates and wait for all {check_count} harness review-check "
            "reports plus their matching current-gate child-completion inbox rows before synthesis.",
            "- Treat the child-completion row as the readiness marker for each reviewer; report "
            "files alone are not completion evidence.",
            "- FULL mode for this review level requires these exact report files in the review "
            "directory:",
            *(f"  - `{name}`" for name in check_reports),
            "- In FULL mode, the gate lead synthesizes these reports; it does not author them itself.",
            "- Do not use native Agent/Task/subagent sidechains for these review-check reports; "
            "the harness seats are the independent reviewer contexts.",
            "- Use SHORT only for the documented short-review exception.",
            "- In each FULL-mode check report, include a plain `Recommended Routing: ...` line.",
        ]
    else:
        review_instruction_lines = [
            "- This is a local L5+ review gate. Complete the review inside this reviewer seat: "
            "perform the independent local review, write `gate-report.md`, then sign the verdict.",
            "- The daemon will not open auxiliary reviewer seats for this gate.",
            "- Write a brief review plan only as your own working outline for the independent "
            "local review. The next routing step after the report is your terminal signal.",
            "- Review the candidate directly against the frozen brief/spec, acceptance or test "
            "package, requirement IDs, and candidate artifact manifest.",
            "- For implementation candidates, run your own testing pass and inspect the produced "
            "work against the frozen constraints.",
            "- For test-author candidates, verify the acceptance package is faithful, runnable or "
            "concrete, traceable, and capable of exposing the intended failures before implementation.",
            "- Treat producer node-root artifacts as candidate evidence; do not overwrite them.",
        ]
    lines = [
        f"# Review Packet — {gate_id}",
        "",
        "<!-- harness-owned: pointer packet for the review gate; do not treat this as evidence by itself -->",
        "",
        f"- Candidate: `{producer_address}`",
        f"- Review seat: `{review_address}`",
        f"- Parent: `{producer_binding.get('parent_address') or ''}`",
        f"- Candidate level: `{producer_binding.get('level') or ''}`",
        f"- Candidate signal identity: `{signal_artifact_identity or 'manual'}`",
        f"- Candidate workspace: `{node_dir}`",
        f"- Review directory: `{gate_dir}`",
        f"- Candidate artifact manifest: `{(manifest_fields or {}).get('gate_candidate_artifact_manifest') or ''}`",
        f"- Candidate artifact manifest sha256: `{(manifest_fields or {}).get('gate_candidate_artifact_manifest_sha256') or ''}`",
        f"- Candidate artifact snapshot directory: `{(manifest_fields or {}).get('gate_candidate_artifact_snapshot_dir') or ''}`",
        "",
        "## Primary Candidate Pointers",
        "",
        f"- Producer report: `{node_dir / 'report.md'}`",
        f"- Producer plan: `{node_dir / 'plan.md'}`",
        f"- Brief/spec pointer: `{node_dir / 'brief.md'}`",
        "- Acceptance/rubric pointer(s):",
        *rubric_pointers,
        "",
        "## Top-Level Candidate Markdown",
        "",
        *(files or ["- (none found)"]),
        "",
        "## Candidate Artifact Manifest",
        "",
        "- The manifest freezes the candidate artifact set and hashes at submission time.",
        "- Snapshot copies under the review directory preserve the exact submitted bytes for "
        "parent/audit proof even if a later accepted handoff rewrites the producer workspace.",
        "- Mutable process bookkeeping such as `plan.md`, `log.md`, `.harness-outbox/`, and "
        "generated packaging/cache metadata is context, not frozen candidate identity.",
        "- Review the candidate named by this manifest. If producer-owned artifacts change after "
        "submission, the current candidate identity is invalid and the producer must submit a fresh "
        "candidate before the gate can route a verdict.",
        f"- Manifest: `{(manifest_fields or {}).get('gate_candidate_artifact_manifest') or gate_dir / _CANDIDATE_ARTIFACT_MANIFEST}`",
        f"- Manifest sha256: `{(manifest_fields or {}).get('gate_candidate_artifact_manifest_sha256') or ''}`",
        f"- Snapshot directory: `{(manifest_fields or {}).get('gate_candidate_artifact_snapshot_dir') or gate_dir / _CANDIDATE_ARTIFACT_SNAPSHOT_DIR}`",
        "",
        "## Trace / Coverage Slice Pointers",
        "",
        "- Best-effort packet slice: this should be replaced by the generated RTM/trace index when available.",
        *(trace or ["- (no trace-bearing or coverage-index markdown found)"]),
        "",
        "## Governing ADR / Decision Pointers",
        "",
        "- Governing context is selected by path scope and name conventions until a decision index exists.",
        *(governing or ["- (no governing decision markdown found in candidate/ancestor scope)"]),
        "",
        "## Interface Contract Pointers",
        "",
        "- Boundary context is selected by interface/contract path conventions until a contract index exists.",
        *(interfaces or ["- (no interface/contract markdown found in candidate/ancestor scope)"]),
        "",
        "## Verification Runtime Pointers",
        "",
        "- These are bounded runtime hints for independent review, not daemon-certified execution "
        "instructions. If a hint fails, record the failure and use the nearest equivalent runtime "
        "rather than trusting the producer report.",
        *(verification or ["- (no local verification runtime hints detected)"]),
        "",
        "## Lower Gate Evidence Pointers",
        "",
        *(lower or ["- (none found under this candidate workspace)"]),
        "",
        "## Expected Lower Execution Evidence",
        "",
        *expected_lower,
        "",
        "## Review Lead Instructions",
        "",
        *review_instruction_lines,
        "- Write the final review-owned gate artifact in this review directory with the `gate-*` basename "
        "for the level (`gate-report.md`, `gate-composition-report.md`, "
        "`gate-area-composition-review.md`, or `gate-composition-review.md`).",
        "- Include a plain literal verdict line in the final gate artifact: `VERDICT: ACCEPT`, "
        "`VERDICT: BOUNCE`, or `VERDICT: ESCALATE`. A `## Verdict` heading or terminal-signal "
        "evidence alone is not enough.",
        "- The gate artifact explains the review outcome. Parent-visible completion happens only after "
        "the harness accepts your terminal signal and routes `gate_passed`; a review artifact alone "
        "does not make the candidate available to the parent.",
        "- In the terminal signal evidence, point to both the producer artifact(s) and the review-owned "
        "gate artifact.",
        "",
    ]
    return "\n".join(lines)


def _accepted_lower_execution_defect(review_address: str, binding: dict, gate_dir: Path) -> Optional[str]:
    producer_address = binding.get("gate_for")
    if not producer_address:
        return None
    producer = ledger.read_binding(producer_address)
    if not producer:
        return None
    if producer.get("child_purpose") == _PLANNING_PURPOSE:
        return None
    expected_level = _EXPECTED_CHILD_LEVEL_BY_PRODUCER_LEVEL.get(
        (producer.get("level") or "").strip()
    )
    if not expected_level:
        return None
    if _gate_artifact_verdict(binding, gate_dir) != "ACCEPT":
        return None

    children = _direct_expected_children(producer_address, expected_level)
    if not children:
        return (
            f"MISSING-LOWER-EXECUTION-EVIDENCE: {review_address} accepted "
            f"{producer_address} without any direct {expected_level} execution child carrying "
            "gate_state='gate_passed'. A higher gate may accept only after the expected lower "
            "execution layer has produced gate-passed child evidence, or after a recorded planning "
            "exception marks the producer as design-only."
        )
    not_passed = [
        str(child.get("node_address") or "")
        for child in children
        if child.get("gate_state") != "gate_passed"
    ]
    if not_passed:
        return (
            f"LOWER-EXECUTION-NOT-GATE-PASSED: {review_address} accepted {producer_address}, "
            f"but direct {expected_level} child candidate(s) are not gate_passed: "
            + ", ".join(sorted(not_passed)[:12])
        )
    return None


def _gate_artifact_verdict(binding: dict, gate_dir: Path) -> Optional[str]:
    artifact_name = _GATE_ARTIFACT_BY_REVIEW_LEVEL.get((binding.get("level") or "").strip())
    if not artifact_name:
        return None
    text = _read(gate_dir / artifact_name)
    match = _GATE_VERDICT.search(text or "")
    return match.group(1).upper() if match else None


def _direct_expected_children(producer_address: str, expected_level: str) -> list[dict]:
    children: list[dict] = []
    try:
        live = ledger.all_nodes()
    except Exception:  # noqa: BLE001 - missing lower evidence is checked best-effort.
        return children
    for address, binding in live.items():
        if not isinstance(binding, dict):
            continue
        if binding.get("parent_address") != producer_address:
            continue
        if (binding.get("level") or "").strip() != expected_level:
            continue
        _path, seat = addressing.split_address(str(address))
        if seat != "exec":
            continue
        if binding.get("child_purpose") in _NON_IMPLEMENTATION_CHILD_PURPOSES:
            continue
        children.append(binding)
    return sorted(children, key=lambda b: str(b.get("node_address") or ""))


def _expected_lower_execution_evidence_lines(producer_address: str, producer_binding: dict) -> list[str]:
    if producer_binding.get("child_purpose") == _PLANNING_PURPOSE:
        return [
            "- This candidate is marked `child_purpose=planning`: it is design-only and is not "
            "expected to carry lower execution child gate passes."
        ]
    expected_level = _EXPECTED_CHILD_LEVEL_BY_PRODUCER_LEVEL.get(
        (producer_binding.get("level") or "").strip()
    )
    if not expected_level:
        return ["- No lower execution evidence rule applies to this candidate level."]
    children = _direct_expected_children(producer_address, expected_level)
    lines = [
        f"- ACCEPT requires at least one direct `{expected_level}` execution child of "
        f"`{producer_address}` with `gate_state=gate_passed`.",
        "- `planning` and `test_author` children are supporting evidence, not implementation evidence.",
    ]
    if not children:
        lines.append(f"- Current direct `{expected_level}` execution children: none found.")
        return lines
    lines.append(f"- Current direct `{expected_level}` execution children:")
    for child in children[:80]:
        lines.append(
            f"  - `{child.get('node_address')}` - state `{child.get('state')}`, "
            f"gate_state `{child.get('gate_state')}`"
        )
    return lines


def _top_level_markdown_pointers(node_dir: Path) -> list[str]:
    try:
        files = sorted(
            p for p in node_dir.iterdir()
            if p.is_file() and p.suffix == ".md" and not p.name.startswith(".")
        )
    except OSError:
        return []
    return [f"- `{p}`" for p in files]


def _acceptance_rubric_pointers(node_dir: Path) -> list[str]:
    """Return existing files that define the candidate's acceptance/rubric surface.

    Executor/workstream candidates usually carry a node-local ``acceptance.md``.
    Product candidates often inherit the acceptance surface from the frozen
    intent/spec and plan-alignment package instead. The review packet should
    name files a reviewer can actually open, not a conventional filename that
    may be absent at that level. Implementation candidates may receive their
    frozen executable acceptance package as a ``tests/`` directory rather than
    as a markdown rubric file; name that directory directly so the review seat
    does not have to rediscover it from the candidate manifest.
    """
    paths: list[Path] = []
    for rel in _RUBRIC_RELATIVE_CANDIDATES:
        path = node_dir / rel
        if path.is_file():
            paths.append(path)

    executable_acceptance = _executable_acceptance_package(node_dir)
    plan_alignment_dir = node_dir / "plan-alignment"
    if plan_alignment_dir.is_dir():
        try:
            paths.extend(sorted(plan_alignment_dir.glob("*.md")))
        except OSError:
            pass

    lines = [f"- `{p}`" for p in _dedupe_paths(paths)[:20]]
    if executable_acceptance is not None:
        lines.append(f"- Frozen executable acceptance package: `{executable_acceptance}`")

    if not lines:
        return [
            "- (no node-local acceptance/rubric file found; use the brief/spec pointer, "
            "candidate report, and lower gate evidence)"
        ]
    return lines


def _executable_acceptance_package(node_dir: Path) -> Optional[Path]:
    tests_dir = node_dir / "tests"
    if not tests_dir.is_dir():
        return None
    try:
        if any(path.is_file() and path.name.startswith("test") for path in tests_dir.rglob("*")):
            return tests_dir
    except OSError:
        return None
    return None


def _trace_coverage_pointers(node_dir: Path) -> list[str]:
    lines: list[str] = []
    inherited_ids = _ids_from_files([node_dir / "brief.md", node_dir / "acceptance.md"])
    if inherited_ids:
        lines.append(
            "- Starter IDs found in brief/acceptance, not the authoritative coverage target: "
            + ", ".join(f"`{i}`" for i in inherited_ids)
        )
        lines.append(
            "- Treat this as an orientation slice. Use the governing ADR/decision and interface "
            "contract pointers below for additional requirement clauses, dotted IDs, and broad "
            "constraints."
        )
    files = []
    for path in _candidate_markdown(node_dir):
        text = _read(path)
        if "trace:" in text or _path_has_hint(path, _TRACE_INDEX_NAME_HINTS):
            files.append(path)
    lines.extend(f"- `{p}`" for p in _dedupe_paths(files)[:80])
    return lines


def _governing_decision_pointers(node_dir: Path) -> list[str]:
    paths: list[Path] = []
    for root in _scope_roots(node_dir):
        paths.extend(_top_level_named_markdown(root, _GOVERNING_NAME_HINTS, _ANCESTOR_GOVERNING_FILES))
        paths.extend(_markdown_under_named_dirs(root, _GOVERNING_NAME_HINTS))
    return [f"- `{p}`" for p in _dedupe_paths(paths)[:80]]


def _interface_contract_pointers(node_dir: Path) -> list[str]:
    paths: list[Path] = []
    for root in _scope_roots(node_dir):
        paths.extend(_top_level_named_markdown(root, _INTERFACE_NAME_HINTS, set()))
        paths.extend(_markdown_under_named_dirs(root, _INTERFACE_NAME_HINTS))
    return [f"- `{p}`" for p in _dedupe_paths(paths)[:80]]


_COMMAND_SECTION_TITLES = {
    "verification command",
    "verification commands",
    "artifact-declared verification command",
    "artifact-declared verification commands",
}
_INLINE_CODE = re.compile(r"`([^`]+)`")
_COMMAND_FENCE_LANGS = {"", "bash", "sh", "shell", "zsh", "console", "terminal"}


def _verification_runtime_pointers(node_dir: Path) -> list[str]:
    lines: list[str] = []
    declared = _declared_verification_commands(node_dir)
    if declared:
        lines.append("- Explicit artifact-declared verification command(s):")
        lines.extend(f"  - `{cmd}`" for cmd in declared[:12])
    else:
        lines.append("- Explicit artifact-declared verification command(s): none found")
        lines.append(
            "  - Parsed only from `Verification Commands` sections in candidate artifacts, "
            "not from prose examples."
        )

    tests_dir = node_dir / "tests"
    python_files = list(tests_dir.rglob("test*.py"))[:1] if tests_dir.is_dir() else []
    python = _first_python_runtime()
    pytest_python = _first_python_with_pytest()
    if python_files or any((node_dir / name).is_file() for name in ("pytest.ini", "pyproject.toml")):
        if pytest_python:
            lines.append(f"- Pytest-capable Python: `{pytest_python} -m pytest`")
        if python:
            lines.append(f"- Stdlib unittest fallback: `{python} -m unittest discover -s tests`")

    package_json = node_dir / "package.json"
    if package_json.is_file():
        for tool in ("npm", "pnpm", "yarn"):
            exe = shutil.which(tool)
            if exe:
                command = "npm test" if tool == "npm" else f"{tool} test"
                lines.append(f"- Node test runner available: `{command}` (`{exe}`)")
                break
    return lines


def _declared_verification_commands(node_dir: Path) -> list[str]:
    commands: list[str] = []
    for name in ("acceptance.md", "brief.md", "report.md"):
        path = node_dir / name
        for section in _verification_command_sections(_read(path)):
            for command in _commands_from_verification_section(section):
                if command not in commands:
                    commands.append(command)
    return commands


def _verification_command_sections(text: str) -> list[str]:
    sections: list[str] = []
    matches = list(_SECTION.finditer(text or ""))
    for idx, match in enumerate(matches):
        title = match.group(1).strip().lower()
        if title not in _COMMAND_SECTION_TITLES:
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text or "")
        sections.append((text or "")[start:end])
    return sections


def _commands_from_verification_section(section: str) -> list[str]:
    commands: list[str] = []
    in_fence = False
    parse_fence = False
    for raw in (section or "").splitlines():
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            if not in_fence:
                parse_fence = _fence_is_command_block(line)
            else:
                parse_fence = False
            in_fence = not in_fence
            continue
        if in_fence and parse_fence:
            command = _clean_declared_command(line)
            if command and command not in commands:
                commands.append(command)
            continue
        if in_fence:
            continue
        lowered = line.lower()
        if lowered.startswith(("verification command:", "command:")):
            command = _clean_declared_command(line.split(":", 1)[1])
            if command and command not in commands:
                commands.append(command)
            continue
        for match in _INLINE_CODE.finditer(line):
            command = _clean_declared_command(match.group(1))
            if command and command not in commands:
                commands.append(command)
    return commands


def _fence_is_command_block(line: str) -> bool:
    marker = "```" if line.startswith("```") else "~~~"
    parts = line[len(marker):].strip().split(None, 1)
    info = parts[0].lower() if parts else ""
    return info in _COMMAND_FENCE_LANGS


def _clean_declared_command(value: str) -> str:
    command = " ".join((value or "").strip().split())
    if command.startswith("$ "):
        command = command[2:].strip()
    if not command or command.startswith("#"):
        return ""
    return command


def _first_python_runtime() -> Optional[str]:
    frozen = os.environ.get("HARNESSD_PYTHON")
    for name in ([frozen] if frozen else []) + ["python3.11", "python3", "python"]:
        exe = shutil.which(name)
        if frozen and name == frozen:
            exe = name if Path(name).is_file() else None
        if exe:
            return str(exe) if frozen and name == frozen else name
    return None


def _first_python_with_pytest() -> Optional[str]:
    frozen = os.environ.get("HARNESSD_PYTHON")
    for name in ([frozen] if frozen else []) + ["python3.11", "python3", "python"]:
        exe = shutil.which(name)
        if frozen and name == frozen:
            exe = name if Path(name).is_file() else None
        if not exe:
            continue
        try:
            proc = subprocess.run(
                [exe, "-c", "import pytest"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return str(exe) if frozen and name == frozen else name
    return None


def _lower_gate_pointers(node_dir: Path) -> list[str]:
    names = {"report.md", "composition-report.md", "area-composition-review.md", "composition-review.md"}
    try:
        files = sorted(
            p for p in node_dir.rglob("*.md")
            if p.is_file() and p.name in names and "reviews" not in p.parts
        )
    except OSError:
        return []
    return [f"- `{p}`" for p in files[:80]]


def _scope_roots(node_dir: Path) -> list[Path]:
    """Candidate node plus ancestors under runtime nodes/.

    Broad ADRs and contracts often govern by path/subtree scope, so review
    packets need ancestor context without asking the producer to author a
    second manifest.
    """
    roots = []
    nodes_root = None
    if ledger.RUNTIME_ROOT is not None:
        nodes_root = Path(ledger.RUNTIME_ROOT) / addressing.NODES_DIRNAME
    for path in [node_dir, *node_dir.parents]:
        if nodes_root is not None and path == nodes_root:
            break
        if nodes_root is not None and nodes_root not in [path, *path.parents]:
            break
        if path.is_dir():
            roots.append(path)
    return roots


def _candidate_markdown(node_dir: Path) -> list[Path]:
    try:
        files = sorted(
            p for p in node_dir.rglob("*.md")
            if p.is_file() and not _is_review_or_hidden_path(p)
        )
    except OSError:
        return []
    return files[:160]


def _top_level_named_markdown(root: Path, hints: tuple[str, ...], always: set[str]) -> list[Path]:
    try:
        files = sorted(
            p for p in root.iterdir()
            if p.is_file()
            and p.suffix == ".md"
            and not p.name.startswith(".")
            and (p.name in always or _path_has_hint(p, hints))
        )
    except OSError:
        return []
    return files


def _markdown_under_named_dirs(root: Path, hints: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    try:
        dirs = sorted(
            p for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and _path_has_hint(p, hints)
        )
    except OSError:
        return []
    for directory in dirs:
        try:
            paths.extend(
                p for p in sorted(directory.rglob("*.md"))
                if p.is_file() and not _is_review_or_hidden_path(p)
            )
        except OSError:
            continue
    return paths


def _ids_from_files(paths: list[Path]) -> list[str]:
    ids = set()
    for path in paths:
        if path.is_file():
            ids.update(_ID_TOKEN.findall(_read(path)))
    return sorted(ids)


def _path_has_hint(path: Path, hints: tuple[str, ...]) -> bool:
    lowered = "/".join(path.parts).lower()
    return any(hint in lowered for hint in hints)


def _is_review_or_hidden_path(path: Path) -> bool:
    return any(part == "reviews" or part.startswith(".") for part in path.parts)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen = set()
    deduped: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
