"""launch_surface — generated launch packets and reference maps from canonical role docs.

The editable source of truth stays in the canonical role/config/template files. Those files carry
inline ``surface:<role> <launch|reference|hidden>`` blocks; this module projects those blocks into
per-spawn artifacts:

* ``.launch-packet.md`` — injected startup surface for pilot roles.
* ``.reference-map.md`` / ``.reference-map.json`` — optional readable references and hidden-surface
  notes.

Pilot scope is deliberately narrow and role-by-role. Roles outside the pilot continue through the
legacy load-manifest path until they get their own co-design pass.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from harnessd import addressing, clock
from harnessd import ledger as ledger_mod
from harnessd import store


VERSION = "launch-surface-v1"
PILOT_ROLES = frozenset({
    "L1",
    "L2",
    "L2+",
    "L3",
    "L3+",
    "L4",
    "L4+",
    "L5",
    "L5+",
    "REVIEW-CHECK",
})

NAMING_CONVENTIONS = (
    "## NAMING CONVENTIONS\n\n"
    "Refer to seats by **AREA NAME** in prose and by canonical node address when precision matters. "
    "Invented aliases are forbidden.\n\n"
    "The system-defined record prefixes and their home surfaces are:\n\n"
    "- `R-` — requirements; home: `client-brief/intent-spec.md`.\n"
    "- `DD-` — architect decisions; home: the L2 decisions record under `L2/decisions/`.\n"
    "- `RN-` — risk notes raised upward; home: a canonical upward message from the discovering seat.\n"
    "- `SD-` — standing rules; home: the standing-rule record of the seat that owns the rule.\n"
    "- `E-` — escalations; home: the escalation artifact of the gate that escalated.\n\n"
    "These are the only identifier schemes. If the work needs a record kind not listed here, "
    "raise that need upward; never mint a local scheme."
)

EXPECTED_SURFACES: dict[str, dict[str, tuple[str, ...]]] = {
    "L1": {
        "launch": (
            "orchestrator-role",
            "intent-guardian",
            "project-child-spawn",
            "plan-alignment",
            "final-fidelity",
            "operating-defaults",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L2": {
        "launch": (
            "architect-role",
            "design-cycle",
            "l3-child-spawn",
            "execution-spine",
            "product-gate",
            "operating-defaults",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L2+": {
        "launch": (
            "review-role",
            "review-method",
            "output-contract",
            "operating-defaults",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L3": {
        "launch": (
            "area-owner-role",
            "planning-execution-modes",
            "execution-spine",
            "coordination-and-gate",
            "operating-defaults",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L3+": {
        "launch": (
            "review-role",
            "review-method",
            "output-contract",
            "operating-defaults",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L4": {
        "launch": (
            "coordinator-role",
            "operating-loop",
            "acceptance-package-rules",
            "execute-review-pair",
            "workstream-gate-contract",
            "durable-work-contracts",
            "workspace-and-authority",
            "operating-defaults",
            "plan-phase-output",
            "brief-craft",
            "monitoring-and-acceptance-ops",
            "pair-management-and-coordination",
            "spawn-process",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L4+": {
        "launch": (
            "review-role",
            "review-method",
            "review-lead-task",
            "short-review-exception",
            "review-checks",
            "bounce-and-escalate",
            "final-output-contract",
            "operating-defaults",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L5": {
        "launch": (
            "executor-role",
            "execute-review-pair",
            "operating-contract",
            "l4-coordination",
            "runtime-stance",
            "defaults-and-craft-kernel",
            "verification-floor",
            "self-inspection",
            "spawn-task-package",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "L5+": {
        "launch": (
            "reviewer-role",
            "review-purpose",
            "review-method",
            "gate-output-contract",
            "review-boundaries",
            "review-outputs",
            "review-runtime-stance",
            "review-self-monitoring",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
    "REVIEW-CHECK": {
        "launch": (
            "check-reviewer-role",
            "check-reviewer-method",
            "check-report-contract",
            "check-boundaries",
        ),
        "reference": ("reference-map-v1",),
        "hidden": ("hidden-surface-v1",),
    },
}

_SURFACE_COMMENT_RE = re.compile(r"<!--\s*/?surface:[^>]*-->", re.S)
_SURFACE_START_RE = re.compile(
    r"<!-- surface:(?P<role>[^ ]+) (?P<kind>launch|reference|hidden) "
    r"id=(?P<id>[^ ]+)(?P<attrs>(?: [^>]*)?)-->",
    re.S,
)
_SURFACE_END_RE = re.compile(
    r"<!-- /surface:(?P<role>[^ ]+) (?P<kind>launch|reference|hidden) id=(?P<id>[^ ]+) -->",
    re.S,
)
_DOC_BLOCK_COMMENT_RE = re.compile(r"<!--\s*/?block:[^>]*-->", re.S)
_DOC_BLOCK_START_RE = re.compile(r"<!-- block:(?P<id>[^ ]+) v(?P<version>[0-9]+) -->")
_DOC_BLOCK_END_RE = re.compile(r"<!-- /block:(?P<id>[^ ]+) -->")


@dataclass(frozen=True)
class SurfaceBlock:
    role: str
    kind: str
    id: str
    source: str
    body: str
    attrs: dict[str, str]


@dataclass(frozen=True)
class _SurfaceRange:
    role: str
    kind: str
    id: str
    source: str
    start: int
    end: int


@dataclass(frozen=True)
class _DocBlockRange:
    id: str
    source: str
    start: int
    end: int


@dataclass(frozen=True)
class LaunchArtifacts:
    version: str
    role: str
    launch_packet_file: str
    launch_packet_hash: str
    launch_surface_source_hash: str
    reference_map_file: str
    reference_map_hash: str
    reference_map_json_file: str
    source_blocks: tuple[dict[str, str], ...]


class LaunchSurfaceError(ValueError):
    """Raised when pilot launch-surface markers are malformed or incomplete."""


def harness_root() -> Path:
    return Path(__file__).resolve().parents[2]


def role_key(level_config) -> str:
    role_variant = str(getattr(level_config, "role_variant", "") or "")
    level = str(getattr(level_config, "level", "") or "")
    if "#review-check" in role_variant:
        return "REVIEW-CHECK"
    token = role_variant or level
    return token.split("#", 1)[0]


def is_pilot(level_config) -> bool:
    return role_key(level_config) in PILOT_ROLES


def source_files(role: str) -> tuple[str, ...]:
    if role == "L1":
        return (
            "operational/L1/role.md",
            "operational/L1/config.md",
        )
    if role == "L2":
        return (
            "operational/L2/role.md",
            "operational/L2/config.md",
            "operational/L2/spawn-template.md",
        )
    if role == "L2+":
        return (
            "operational/L2+/role.md",
            "operational/L2+/config.md",
        )
    if role == "L3":
        return (
            "operational/L3/role.md",
            "operational/L3/config.md",
            "operational/L3/spawn-template.md",
        )
    if role == "L3+":
        return (
            "operational/L3+/role.md",
            "operational/L3+/config.md",
        )
    if role == "L4":
        return (
            "operational/L4/role.md",
            "operational/L4/config.md",
            "operational/L4/spawn-template.md",
        )
    if role == "L4+":
        return (
            "operational/L4+/role.md",
            "operational/L4+/config.md",
        )
    if role == "L5":
        return (
            "operational/L5/role.md",
            "operational/L5/config.md",
            "operational/L5/spawn-template.md",
        )
    if role == "L5+":
        return (
            "operational/L5+/role.md",
            "operational/L5+/config.md",
        )
    if role == "REVIEW-CHECK":
        return (
            "operational/shared/review-handbook.md",
        )
    return ()


def surface_blocks(role: str, *, kind: str | None = None, root: Path | None = None) -> list[SurfaceBlock]:
    root = root or harness_root()
    blocks = _validated_surface_blocks(role, root=root)
    if kind is not None:
        blocks = [block for block in blocks if block.kind == kind]
    return blocks


def validate(role: str, *, root: Path | None = None) -> None:
    """Fail loudly if the pilot surface marker set is not exactly the expected manifest."""
    _validated_surface_blocks(role, root=root or harness_root())


def materialize(
    node_address: str,
    level_config,
    spawn_brief: dict[str, Any],
    *,
    runtime_root=None,
) -> LaunchArtifacts | None:
    """Generate launch/reference artifacts for pilot roles and return their metadata.

    ``spawn_brief`` is the dict handed to the runtime adapter. It is not mutated by this function;
    the chokepoint copies the returned metadata onto the adapter brief and the binding.
    """
    role = role_key(level_config)
    if role not in PILOT_ROLES:
        return None
    runtime_root = runtime_root if runtime_root is not None else ledger_mod.RUNTIME_ROOT
    workspace = spawn_brief.get("workspace")
    if not workspace and runtime_root is not None:
        workspace = str(addressing.node_dir(node_address, runtime_root))
    if not workspace:
        return None

    node_dir = Path(str(workspace))
    node_dir.mkdir(parents=True, exist_ok=True)

    blocks = _validated_surface_blocks(role)
    launch_blocks = [block for block in blocks if block.kind == "launch"]
    reference_blocks = [block for block in blocks if block.kind == "reference"]
    hidden_blocks = [block for block in blocks if block.kind == "hidden"]
    dynamic_sections = _dynamic_sections(role, node_address, node_dir, spawn_brief, runtime_root)

    launch_text = _render_launch_packet(role, node_address, launch_blocks, dynamic_sections)
    root = harness_root()
    reference_text = _render_reference_map(role, node_address, reference_blocks, hidden_blocks, root=root)
    reference_json = _reference_json(role, node_address, reference_blocks, hidden_blocks, root=root)

    launch_path = node_dir / ".launch-packet.md"
    ref_path = node_dir / ".reference-map.md"
    ref_json_path = node_dir / ".reference-map.json"
    store.atomic_replace(launch_path, lambda h: h.write(launch_text))
    store.atomic_replace(ref_path, lambda h: h.write(reference_text))
    store.atomic_replace(ref_json_path, lambda h: h.write(json.dumps(reference_json, indent=2) + "\n"))

    return LaunchArtifacts(
        version=VERSION,
        role=role,
        launch_packet_file=str(launch_path),
        launch_packet_hash=_sha256(launch_text),
        launch_surface_source_hash=_source_hash(role, blocks),
        reference_map_file=str(ref_path),
        reference_map_hash=_sha256(reference_text),
        reference_map_json_file=str(ref_json_path),
        source_blocks=tuple(
            {"kind": b.kind, "id": b.id, "source": b.source}
            for b in (*launch_blocks, *reference_blocks, *hidden_blocks)
        ),
    )


def _validated_surface_blocks(role: str, *, root: Path | None = None) -> list[SurfaceBlock]:
    if role not in PILOT_ROLES:
        return []
    root = root or harness_root()
    blocks: list[SurfaceBlock] = []
    errors: list[str] = []
    for rel in source_files(role):
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{rel}: unreadable surface source: {exc}")
            continue
        file_blocks, file_errors, surface_ranges = _parse_surface_file(role, rel, text)
        blocks.extend(file_blocks)
        errors.extend(file_errors)
        errors.extend(_doc_overlap_errors(rel, text, surface_ranges))

    counts = Counter((block.kind, block.id) for block in blocks)
    expected = {
        (kind, block_id)
        for kind, ids in EXPECTED_SURFACES[role].items()
        for block_id in ids
    }
    seen = set(counts)
    for kind, block_id in sorted(expected - seen):
        errors.append(f"{role}: missing surface block {kind} id={block_id}")
    for kind, block_id in sorted(seen - expected):
        errors.append(f"{role}: unexpected surface block {kind} id={block_id}")
    for kind, block_id in sorted(expected & seen):
        if counts[(kind, block_id)] != 1:
            errors.append(
                f"{role}: duplicate surface block {kind} id={block_id} "
                f"(count={counts[(kind, block_id)]})"
            )

    if errors:
        raise LaunchSurfaceError("launch surface validation failed:\n" + "\n".join(errors))
    return blocks


def _parse_surface_file(
    expected_role: str,
    rel: str,
    text: str,
) -> tuple[list[SurfaceBlock], list[str], list[_SurfaceRange]]:
    blocks: list[SurfaceBlock] = []
    ranges: list[_SurfaceRange] = []
    errors: list[str] = []
    stack: list[tuple[re.Match[str], int, int]] = []

    for match in _SURFACE_COMMENT_RE.finditer(text):
        raw = match.group(0)
        start_match = _SURFACE_START_RE.fullmatch(raw)
        end_match = _SURFACE_END_RE.fullmatch(raw)
        if start_match is None and end_match is None:
            errors.append(f"{rel}:{_line_no(text, match.start())}: malformed surface marker: {raw}")
            continue
        if start_match is not None:
            if stack:
                prior, _, _ = stack[-1]
                errors.append(
                    f"{rel}:{_line_no(text, match.start())}: nested surface marker "
                    f"{start_match.group('kind')} id={start_match.group('id')} inside "
                    f"{prior.group('kind')} id={prior.group('id')}"
                )
            stack.append((start_match, match.end(), match.start()))
            continue

        assert end_match is not None
        if not stack:
            errors.append(
                f"{rel}:{_line_no(text, match.start())}: closing surface marker without opener: {raw}"
            )
            continue
        start, body_start, start_pos = stack[-1]
        if (
            end_match.group("role") != start.group("role")
            or end_match.group("kind") != start.group("kind")
            or end_match.group("id") != start.group("id")
        ):
            errors.append(
                f"{rel}:{_line_no(text, match.start())}: surface close does not match opener "
                f"{start.group('role')} {start.group('kind')} id={start.group('id')}: {raw}"
            )
            continue
        stack.pop()
        block = SurfaceBlock(
            role=start.group("role"),
            kind=start.group("kind"),
            id=start.group("id"),
            source=rel,
            body=text[body_start:match.start()].strip(),
            attrs=_parse_attrs(start.group("attrs") or ""),
        )
        if block.role != expected_role:
            errors.append(
                f"{rel}:{_line_no(text, match.start())}: unexpected surface role "
                f"{block.role!r}; expected {expected_role!r}"
            )
        blocks.append(block)
        ranges.append(
            _SurfaceRange(
                role=block.role,
                kind=block.kind,
                id=block.id,
                source=rel,
                start=start_pos,
                end=match.end(),
            )
        )

    for start, _, start_pos in reversed(stack):
        errors.append(
            f"{rel}:{_line_no(text, start_pos)}: unclosed surface marker "
            f"{start.group('kind')} id={start.group('id')}"
        )
    return blocks, errors, ranges


def _doc_overlap_errors(rel: str, text: str, surfaces: list[_SurfaceRange]) -> list[str]:
    errors: list[str] = []
    doc_blocks = _doc_block_ranges(rel, text)
    for surface in surfaces:
        for doc in doc_blocks:
            if surface.end <= doc.start or doc.end <= surface.start:
                continue
            if surface.start <= doc.start and doc.end <= surface.end:
                continue
            errors.append(
                f"{rel}:{_line_no(text, surface.start)}: unsafe overlap between surface "
                f"{surface.kind} id={surface.id} and doc-system block id={doc.id}; "
                "a surface may contain a whole doc-system block, but may not start or end inside it"
            )
    return errors


def _doc_block_ranges(rel: str, text: str) -> list[_DocBlockRange]:
    ranges: list[_DocBlockRange] = []
    open_marker: tuple[re.Match[str], int] | None = None
    for match in _DOC_BLOCK_COMMENT_RE.finditer(text):
        raw = match.group(0)
        start_match = _DOC_BLOCK_START_RE.fullmatch(raw)
        end_match = _DOC_BLOCK_END_RE.fullmatch(raw)
        if start_match is not None:
            open_marker = (start_match, match.start())
            continue
        if end_match is not None and open_marker is not None:
            start, start_pos = open_marker
            if end_match.group("id") == start.group("id"):
                ranges.append(_DocBlockRange(id=start.group("id"), source=rel, start=start_pos, end=match.end()))
            open_marker = None
    return ranges


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, quoted, bare in re.findall(r'([A-Za-z_][A-Za-z0-9_-]*)=(?:"([^"]*)"|([^ ]+))', raw):
        attrs[key] = quoted or bare
    return attrs


def _dynamic_sections(role: str, node_address: str, node_dir: Path, spawn_brief: dict[str, Any], runtime_root) -> list[str]:
    sections: list[str] = []
    startup_section = _render_startup_sequence(role, node_address)
    if startup_section:
        sections.append(startup_section)
    runtime_section = _render_runtime_commands(role, runtime_root)
    if runtime_section:
        sections.append(runtime_section)
    sections.append("## Task Package\n")
    for label, path in _task_files(role, node_address, node_dir, spawn_brief, runtime_root):
        sections.append(_render_file_section(label, path))
    sections.append(_render_signoff(node_address, runtime_root))
    return sections


def _render_startup_sequence(role: str, node_address: str) -> str:
    if role == "REVIEW-CHECK":
        seat = addressing.split_address(node_address)[1]
        task_seed = _startup_task_list_seed(role)
        return (
            "## Startup Task List Seed\n\n"
            "Once this launch packet is in context, your first operational act is to create or refresh "
            "the native task list for this runtime (Claude Code todo list / Codex plan tool). Keep "
            "it to a few high-level review steps for this single check; do not turn it into a script "
            "of commands or broad file reads. If the task-list tool is deferred or not yet callable, "
            "use only the runtime's tool-discovery action to expose that task-list tool family first, "
            "then create the initial list from the seed before any file reads or workspace inspection. "
            "A suitable initial seed is:\n\n"
            f"{task_seed}\n"
            "Review-check seats do not fill the final gate artifact and normally do not fill `plan.md`; "
            "their durable output is the assigned check report.\n\n"
            "## Startup Sequence\n\n"
            "1. Create or refresh the native task list from the seed above. If needed, first discover "
            "only the task-list tool family.\n"
            f"2. Orient on the current `.inbox.{seat}.jsonl` task/event rows.\n"
            "3. Read the review-check brief in the Task Package before opening optional references.\n"
            "4. Write only the assigned check report. Do not write the final gate artifact and do "
            "not render ACCEPT/BOUNCE/ESCALATE for the candidate.\n"
        )
    seat = addressing.split_address(node_address)[1]
    task_seed = _startup_task_list_seed(role)
    if role.endswith("+"):
        return (
            "## Startup Task List Seed\n\n"
            "Once this launch packet is in context, your first operational act is to create or refresh "
            "the native task list for this runtime (Claude Code todo list / Codex plan tool). Use "
            "high-level review-management items at this gate's altitude; do not turn the list into a "
            "script of commands, report reads, or probes. If the task-list tool is deferred or not yet "
            "callable, use only the runtime's tool-discovery action to expose that task-list tool "
            "family first, then create the initial list from the seed before any file reads or workspace "
            "inspection. A suitable initial seed is:\n\n"
            f"{task_seed}\n"
            "After bounded orientation on the review packet, use the required review artifact for the "
            "durable review plan: L4+/L3+/L2+ write `reviews/<gate-id>/review-plan.md`; L5+ writes "
            "its local review notes/gate artifact as directed by its packet.\n\n"
            "## Startup Sequence\n\n"
            "1. Create or refresh the native task list from the seed above. If needed, first discover "
            "only the task-list tool family.\n"
            f"2. Orient on the current `.inbox.{seat}.jsonl` task/event rows.\n"
            "3. Orient on the review packet in the Task Package. Do only the bounded orientation needed "
            "to make the review plan concrete.\n"
            "4. Refresh the native task list if orientation changes the review shape.\n"
            "5. For L4+/L3+/L2+ FULL mode, write `reviews/<gate-id>/review-plan.md` before synthesis; "
            "for L5+, proceed with the local gate artifact named by the packet.\n"
            "6. Use the launch surfaces and review packet as the normal authority. Open `.reference-map.md` "
            "only for a concrete lookup named by the packet, a rejection reason, a return-contract defect, "
            "or a gate/coordination event.\n"
        )
    return (
        "## Startup Task List Seed\n\n"
        "Once this launch packet is in context, your first operational act is to create or refresh "
        "the native task list for this runtime (Claude Code todo list / Codex plan tool). Use "
        "high-level items at this role's altitude; do not turn the list into a script of commands "
        "or file reads. If the task-list tool is deferred or not yet callable, use only the "
        "runtime's tool-discovery action to expose that task-list tool family first, then create "
        "the initial list from the seed before any file reads or workspace inspection. A suitable "
        "initial seed is:\n\n"
        f"{task_seed}\n"
        "After the bounded orientation in the startup sequence, refresh the task list and mirror "
        "it into `plan.md` as the durable copy.\n\n"
        "## Startup Sequence\n\n"
        "1. Create or refresh the native task list from the seed above. If needed, first discover "
        "only the task-list tool family.\n"
        f"2. Orient on the current `.inbox.{seat}.jsonl` task/event rows.\n"
        "3. Orient on the task package files named below. Do only the bounded orientation needed to make "
        "the native task list and durable plan concrete.\n"
        "4. Refresh the native task list if orientation changes the work shape.\n"
        "5. Fill or update the preinstantiated `plan.md` form as the durable mirror before "
        "substantive work or child spawning. When updating preinstantiated forms such as "
        "`plan.md` or `report.md`, first open the form through the runtime's file-read tool so "
        "the editor has the current file state.\n"
        "6. Use the launch surfaces and task package as the normal authority for the task. Open "
        "`.reference-map.md` only for a concrete lookup named by the task, a rejection reason, a "
        "return-contract defect, or a gate/coordination event.\n"
    )


def _startup_task_list_seed(role: str) -> str:
    if role == "REVIEW-CHECK":
        items = [
            "Orient on the assigned check brief, review packet, inbox, and report path.",
            "Evaluate the assigned review brief at the gate altitude.",
            "Write the assigned check report with evidence pointers and recommended routing.",
            "Verify the report path and sign off.",
        ]
    elif role == "L4":
        items = [
            "Orient on the workstream brief, acceptance, inbox, and frozen contract.",
            "Define the L5 child sequence at workstream altitude.",
            "Open the test-author L5 and wait for L5+ package review before implementation.",
            "Open the implementation L5 against the accepted test package and wait for L5+ review.",
            "Integrate child gate evidence, then fill report.md, verify IDs, and sign off.",
        ]
    elif role == "L4+":
        items = [
            "Orient on the review packet, L3 brief, L4 report, and lower gate pointers.",
            "Write the workstream-composition review plan and choose FULL or the recorded SHORT exception.",
            "Wait until every selected review check has both its report and matching current-gate child-completion row; report files alone are not readiness.",
            "Synthesize findings at workstream altitude and write the gate artifact.",
            "Sign the gate verdict with evidence pointers.",
        ]
    elif role == "L3":
        items = [
            "Orient on the area brief, frozen design or planning task, acceptance, and inbox.",
            "Define or drive the L4 workstream sequence at area altitude.",
            "Wait for workstream gate routes before integration.",
            "Resolve area-owned coordination issues or escalate authority gaps.",
            "Integrate workstream evidence, then fill report.md, verify IDs, and sign off.",
        ]
    elif role == "L3+":
        items = [
            "Orient on the review packet, area design/report, workstream reports, and L4+ gate pointers.",
            "Write the area-composition review plan and choose FULL or the recorded SHORT exception.",
            "Wait until every selected review check has both its report and matching current-gate child-completion row; report files alone are not readiness.",
            "Synthesize findings at area altitude and write the gate artifact.",
            "Sign the gate verdict with evidence pointers.",
        ]
    elif role == "L2":
        items = [
            "Orient on the project brief, client intent package, acceptance, and inbox.",
            "Own architecture, ADRs, area decomposition, and plan-alignment submission.",
            "Wait for planning/design gate routes before freezing the build package.",
            "Drive execution areas after plan-alignment PASS.",
            "Integrate project evidence, then fill report.md, verify IDs, and sign off.",
        ]
    elif role == "L2+":
        items = [
            "Orient on the review packet, intent/spec, architecture, area reports, and L3+ gate pointers.",
            "Write the product-composition review plan and choose FULL or the recorded SHORT exception.",
            "Wait until every selected review check has both its report and matching current-gate child-completion row; report files alone are not readiness.",
            "Synthesize findings at product altitude and write the gate artifact.",
            "Sign the gate verdict with evidence pointers.",
        ]
    elif role == "L1":
        items = [
            "Orient on the intake, inbox, and project status at intent altitude.",
            "Author or validate the client-facing intent package.",
            "Spawn or supervise the L2 project path and run plan alignment when submitted.",
            "Evaluate final project return against the original user intent.",
            "Fill report.md, verify IDs, and sign off or surface the result to the user.",
        ]
    elif role == "L5":
        items = [
            "Orient on the task brief, acceptance, inbox, and local files.",
            "Produce only this bounded task's artifact.",
            "Run the stated verification plus obvious deterministic local checks.",
            "Capture evidence in report.md.",
            "Verify requirement-ID citations and sign off.",
        ]
    elif role == "L5+":
        items = [
            "Orient on the review packet, producer report, acceptance, and candidate evidence.",
            "Review independently against the task spec and local quality criteria.",
            "Run necessary verification for this local gate.",
            "Write the gate report with a literal verdict line.",
            "Sign the gate verdict with evidence pointers.",
        ]
    else:
        items = [
            "Orient on the launch packet, task package, and inbox.",
            "Do the bounded work assigned to this seat.",
            "Capture evidence in report.md.",
            "Verify requirement-ID citations and sign off.",
        ]
    return "\n".join(f"- [ ] {item}" for item in items)


def _render_runtime_commands(role: str, runtime_root) -> str:
    if role not in {"L1", "L2", "L3", "L4"} or runtime_root is None:
        return ""
    root = harness_root()
    socket = Path(runtime_root) / ".harnessd" / "harnessd.sock"
    header = "Runtime Paths For L1 Control Verbs" if role == "L1" else "Runtime Paths For Control Verbs"
    role_note = (
        "Normal project-child spawning is not a `harnessctl` verb; it uses the `.harness-outbox/` "
        "request file described in the L1 project-child spawn surface."
        if role == "L1"
        else "Normal child spawning is not a `harnessctl` verb; it uses this node's `.harness-outbox/` request files."
    )
    return (
        f"## {header}\n\n"
        f"- Harness root: `{root}`\n"
        f"- Runtime root: `{runtime_root}`\n"
        "- Python command: `python3`\n"
        "- When this launch packet tells you to use a `harnessctl` control-plane verb, invoke it as:\n"
        f"  `cd {root} && HARNESSD_SOCKET={socket} python3 -m harnessd.harnessctl <verb> ...`\n"
        f"- {role_note}\n"
    )


def _task_files(role: str, node_address: str, node_dir: Path, spawn_brief: dict[str, Any], runtime_root) -> Iterable[tuple[str, Path]]:
    if role == "REVIEW-CHECK":
        spec = _best_path(node_dir, spawn_brief.get("spec_pointer"), "brief.md")
        yield "review-check-brief.md", spec
        packet = _review_packet_path(node_address, runtime_root)
        if packet is not None:
            yield "review-packet.md", packet
        return
    if role.endswith("+"):
        packet = _review_packet_path(node_address, runtime_root)
        if packet is not None:
            yield "review-packet.md", packet
        return
    yield "brief.md", _best_path(node_dir, spawn_brief.get("spec_pointer"), "brief.md")
    yield "acceptance.md", _best_path(node_dir, spawn_brief.get("frozen_acceptance_ref"), "acceptance.md")
    if role == "L2":
        for name in (
            "raw-request.md",
            "intent-spec.md",
            "vision.md",
            "priorities.md",
        ):
            yield f"client-brief/{name}", node_dir / "client-brief" / name


def _best_path(node_dir: Path, pointer: Any, fallback_name: str) -> Path:
    fallback = node_dir / fallback_name
    if fallback.exists():
        return fallback
    if pointer:
        p = Path(str(pointer))
        if p.is_absolute() and p.exists():
            return p
    return fallback


def _review_packet_path(node_address: str, runtime_root) -> Path | None:
    if runtime_root is None:
        return None
    try:
        binding = ledger_mod.read_binding(node_address) or {}
        if binding.get("gate_review_packet"):
            return Path(str(binding["gate_review_packet"]))
        producer_address = binding.get("gate_for")
        if producer_address:
            producer = ledger_mod.read_binding(producer_address) or {}
            packet = producer.get("gate_review_packet")
            if packet:
                return Path(str(packet))
        node_dir = addressing.node_dir(node_address, runtime_root)
        candidates = sorted((node_dir / "reviews").glob("*/review-packet.md"))
        return candidates[-1] if candidates else None
    except Exception:  # noqa: BLE001 - launch generation should tolerate absent review state here.
        return None


def _render_file_section(label: str, path: Path) -> str:
    if path.exists():
        body = path.read_text(encoding="utf-8", errors="replace").strip()
        return f"## {label}\n\nPath: `{path}`\n\n```md\n{body}\n```\n"
    return f"## {label}\n\nPath: `{path}`\n\nFile was not present when the launch packet was generated.\n"


def _render_signoff(node_address: str, runtime_root) -> str:
    if runtime_root is None:
        return "## Sign-off\n\nNo runtime root was bound when the launch packet was generated.\n"
    signoff = addressing.signoff_path(node_address, runtime_root)
    signal = addressing.signal_path(node_address, runtime_root)
    try:
        payload = json.loads(signoff.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    owner = payload.get("owner_token", "<owner_token from sign-off file>")
    return (
        "## Sign-off\n\n"
        f"- Read-only sign-off handshake file: `{signoff}`\n"
        f"- Terminal signal file you write: `{signal}`\n"
        f"- Read the handshake file and copy `owner_token` verbatim: `{owner}`\n"
        "- Do not create, edit, or overwrite the sign-off handshake file; it is runtime-owned.\n"
        "- Terminal signal JSON uses `signal`, not `status`: `DONE` or `FAILED`.\n"
        "- If blocked, submit a canonical direct-edge message to your parent with "
        "`needs_answer: true` and park without a terminal signal.\n"
        "- Set `ts` to the exact current UTC instant immediately before writing the signal. "
        "Use `date -u +%Y-%m-%dT%H:%M:%SZ` or an equivalent clock read; do not invent, "
        "round, or copy an example timestamp.\n"
        "- Include an `evidence` object with the report, verdict, gate, completion, or failure "
        "details relevant to this seat.\n"
    )


def _render_launch_packet(role: str, node_address: str, blocks: list[SurfaceBlock], dynamic_sections: list[str]) -> str:
    lines = [
        f"# Launch Packet — {node_address}",
        "",
        f"- role: `{role}`",
        f"- generated_at: `{clock.now_utc()}`",
        f"- version: `{VERSION}`",
        "",
        "This packet is generated from canonical `surface:<role> launch` blocks plus the node-local task package.",
        "It is the startup surface for normal work. Use `.reference-map.md` only for concrete lookups.",
        "",
        "---",
        "",
        NAMING_CONVENTIONS,
        "",
    ]
    for block in blocks:
        lines.extend(
            [
                "---",
                "",
                f"<!-- source: {block.source}::{block.id} -->",
                "",
                block.body,
                "",
            ]
        )
    for section in dynamic_sections:
        lines.extend(["---", "", section.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def _render_reference_map(
    role: str,
    node_address: str,
    refs: list[SurfaceBlock],
    hidden: list[SurfaceBlock],
    *,
    root: Path,
) -> str:
    lines = [
        f"# Reference Map — {node_address}",
        "",
        f"- role: `{role}`",
        f"- generated_at: `{clock.now_utc()}`",
        f"- version: `{VERSION}`",
        f"- harness_root: `{root}`",
        "",
        "These are optional readable references, not startup reading.",
        "Repo-relative paths below are resolved under `harness_root`; use the resolved paths when reading from a node workspace.",
    ]
    if hidden:
        lines.append(
            "Hidden-surface rules are retained in `.reference-map.json` for harness and maintainer use, not shown here."
        )
    lines.extend(["", "## Reference Surface", ""])
    lines.extend(_block_lines(refs, root=root) or ["- (no reference surface declared)"])
    return "\n".join(lines).rstrip() + "\n"


def _block_lines(blocks: list[SurfaceBlock], *, root: Path) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        lines.append(f"<!-- source: {block.source}::{block.id} -->")
        lines.extend(block.body.splitlines())
        resolved = _resolved_paths(block, root=root)
        if resolved:
            lines.extend(["", "Resolved reference paths:"])
            for rel, abs_path in resolved:
                lines.append(f"- `{rel}` -> `{abs_path}`")
        lines.append("")
    return lines


def _reference_json(
    role: str,
    node_address: str,
    refs: list[SurfaceBlock],
    hidden: list[SurfaceBlock],
    *,
    root: Path,
) -> dict[str, Any]:
    return {
        "version": VERSION,
        "role": role,
        "node_address": node_address,
        "harness_root": str(root),
        "generated_at": clock.now_utc(),
        "references": [_json_block(block, root=root) for block in refs],
        "hidden": [_json_block(block, root=root) for block in hidden],
    }


def _source_hash(role: str, blocks: list[SurfaceBlock]) -> str:
    payload = {
        "version": VERSION,
        "role": role,
        "blocks": [
            {
                "source": block.source,
                "kind": block.kind,
                "id": block.id,
                "attrs": dict(sorted(block.attrs.items())),
                "body": block.body,
            }
            for block in blocks
        ],
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _reference_paths(block: SurfaceBlock) -> list[str]:
    return sorted(set(re.findall(r"`([^`]+\.md)`", block.body)))


def _resolved_paths(block: SurfaceBlock, *, root: Path) -> list[tuple[str, str]]:
    resolved: list[tuple[str, str]] = []
    for rel in _reference_paths(block):
        path = Path(rel)
        if path.is_absolute():
            resolved.append((rel, str(path)))
        else:
            resolved.append((rel, str(root / path)))
    return resolved


def _json_block(block: SurfaceBlock, *, root: Path) -> dict[str, Any]:
    paths = _reference_paths(block)
    return {
        "id": block.id,
        "source": block.source,
        "body": block.body,
        "paths": paths,
        "resolved_paths": [
            {"path": rel, "absolute_path": abs_path}
            for rel, abs_path in _resolved_paths(block, root=root)
        ],
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
