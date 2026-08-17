"""brief — the runtime-NEUTRAL task contract + the per-spawn load-manifest, and the resume DELTA brief.

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.11 (the FROZEN brief.py interface):
        assemble_neutral(node_address, level_config, work_node) -> NeutralContract
        delta_brief(node_address, prior_incarnation, work_node, changes) -> DeltaBrief
  - IMPLEMENTATION-PLAN Increment-10 role-as-documents note (L770-777) + DAEMON §6.1 STEP2 +
    §6.3 (the runtime-neutral contract) + §6.4 (the delta brief).
  - design/ROLE-RESOLUTION.md §2 (the per-seat LOAD-MANIFEST tiers) + §3 (read-in-place against
    the harness root) + §4 (assembled from role_variant at STEP2).

THE HEADLINE (role-as-documents, H40 / Decision B): ``assemble_neutral`` writes the per-spawn
load-manifest ("Identity — Load These Documents") INTO the node — selected by ``role_variant`` —
listing the role docs the agent READS IN PLACE as PATHS relative to the harness root
(``operational/L{n}/{soul,role,config}.md`` + the always-loaded ``operational/shared/*.md`` contract
docs + level extras). AMENDED 2026-06-12 (user ruling, LR-4 cure): the per-level
IDENTITY TRIO (soul/role/config — ``identity_docs``) is now AUTO-LOADED by the adapters
(CC: flattened into a per-spawn composed system prompt; codex: listed first in the boot
prompt) — identity acquisition no longer rides on agent diligence. The manifest still names
ALL docs as PATHS (the agent re-reads in place mid-run; shared protocol docs remain
read-in-place only), and the SHARED prompt (``config.SYSTEM_PROMPT_FILE``) leads the
composed bundle.

The runtime-NEUTRAL contract (§6.3) carries identity/address, the spec pointer, the frozen-acceptance
reference, interface/constraints/workspace/reporting — IDENTICAL across runtimes; the adapter injects
ONLY the three runtime-specific things (tool manifest, harness invocation, output format) over this
neutral core.

BUILDER DECISIONS (the §2.11 shapes the frozen tests leave open — stated in the build report):

  * NeutralContract / DeltaBrief SHAPE — frozen dataclasses (stdlib, no new dep), repr-introspectable
    (the tests scan ``repr(contract)`` + a best-effort ``load_manifest`` accessor). NeutralContract
    carries: ``node_address``, ``level``, ``role_variant``, ``system_prompt_file`` (the shared
    constant), ``load_manifest`` (the ordered list of role-doc PATHS), ``spec_pointer``,
    ``frozen_acceptance_ref``, ``workspace``, ``status_md`` / ``log_md`` / ``report_md`` pointers,
    and ``reporting`` expectations. DeltaBrief carries: ``node_address``, ``prior_incarnation``,
    ``changes``, the durable work-node pointers, and a ``delta`` summary — a DELTA, never the full
    original brief. FORK-BRIEF-SHAPE: the precise field set is the builder's spec-faithful choice;
    the load-bearing facts (address + acceptance ref present; manifest is paths selected by
    role_variant; delta names what changed + points at the work node) are pinned by the tests.

  * MANIFEST CONTENT — assembled from ROLE-RESOLUTION §2's tiers, keyed off ``role_variant`` /
    ``level``. Each entry is a harness-root-relative PATH string (never the file body). The
    per-level docs are ``operational/L{n}/{soul,role,config}.md``; the level extras + the
    always-loaded shared contract docs follow §2's table. The manifest is purely a list of paths —
    the agent reads them in place (§3); brief.py does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from harnessd import config

# ---------------------------------------------------------------------------
# The always-loaded SHARED contract docs (ROLE-RESOLUTION §2 tier "Shared — always").
# Paths relative to the harness root; the agent reads them in place (no flatten).
# ``system-prompt.md`` is the shared base prompt, not a read-doc, so it is NOT listed among the
# read-manifest docs here. Claude-Code seats receive it inside the composed `.identity-prompt.md`.
# ---------------------------------------------------------------------------

_SHARED_ALWAYS: tuple[str, ...] = (
    "operational/shared/comms-protocol.md",
    "operational/shared/agent-lifecycle.md",
    "operational/shared/runtime-and-model-map.md",
)

# agent-definition-principles.md scopes itself to the definition-authoring levels (L1–L4); L5 omits it.
_DEFINITION_AUTHORING_DOC = "operational/shared/agent-definition-principles.md"

# git-protocol.md is loaded by levels that own code movement and gate evidence; its header scopes it.
_GIT_PROTOCOL_DOC = "operational/shared/git-protocol.md"

# Per-level EXTRA docs (ROLE-RESOLUTION §2 "Per-level" extras rows), keyed by level token.
_LEVEL_EXTRAS: dict[str, tuple[str, ...]] = {
    "L1": ("operational/L1/handbook.md", "operational/L1/intake-session-template.md"),
    "L2+": (
        "operational/shared/review-handbook.md",
        "operational/shared/templates/higher-gate-review-plan-template.md",
        "operational/shared/templates/check-review-report-template.md",
        "operational/shared/templates/product-composition-review-template.L2+.md",
        "design/GATE-LIFECYCLE.md",
        "design/HIGHER-LEVEL-GATES.md",
        "design/QUALITY-GATE.md",
    ),
    "L3": ("operational/L3/planning-template.md",),
    "L3+": (
        "operational/shared/review-handbook.md",
        "operational/shared/templates/higher-gate-review-plan-template.md",
        "operational/shared/templates/check-review-report-template.md",
        "operational/shared/templates/area-composition-review-template.L3+.md",
        "design/GATE-LIFECYCLE.md",
        "design/HIGHER-LEVEL-GATES.md",
        "design/QUALITY-GATE.md",
    ),
    "L4+": (
        "operational/shared/review-handbook.md",
        "operational/shared/templates/higher-gate-review-plan-template.md",
        "operational/shared/templates/check-review-report-template.md",
        "operational/shared/templates/workstream-composition-review-template.L4+.md",
        "design/GATE-LIFECYCLE.md",
        "design/HIGHER-LEVEL-GATES.md",
        "design/QUALITY-GATE.md",
    ),
    "L5": ("operational/L5/swe-handbook.md",),
}


def _level_token(level_config) -> str:
    """Resolve the level token (L1..L5) the per-level role docs are keyed by.

    ``role_variant`` may carry a seat suffix (``L5#exec``, ``L3#plan``); the per-level *file* docs
    live under ``operational/L{n}/`` keyed by the BARE level token, so we split on '#'. Falls back
    to ``level`` when ``role_variant`` is absent.
    """
    role_variant = getattr(level_config, "role_variant", None)
    level = getattr(level_config, "level", None)
    token = role_variant or level or ""
    # Seat-variant suffix (#exec / #review / #plan) selects the manifest but not the level dir.
    return token.split("#", 1)[0]


def identity_docs(level: str) -> list:
    """The per-level IDENTITY trio (soul, role, config) as harness-root-relative paths.

    The ONE source of truth for "what makes this seat itself" — used by the load-manifest
    below AND by the adapters' identity AUTO-LOAD (LR-4 cure, user ruling 2026-06-12: the
    trio is flattened into the CC per-spawn system prompt / listed in the codex boot prompt,
    so identity acquisition never rides on agent diligence). Shared protocol docs stay
    read-in-place via the manifest (reference library, not identity).
    """
    return [
        f"operational/{level}/soul.md",
        f"operational/{level}/role.md",
        f"operational/{level}/config.md",
    ]


def _assemble_load_manifest(level_config) -> list[str]:
    """Assemble the per-seat load-manifest (ROLE-RESOLUTION §2) as a list of harness-root PATHS.

    Selected by ``role_variant`` (via the level token): the per-level
    ``operational/L{n}/{soul,role,config}.md`` + the level extras, then the always-loaded shared
    contract docs (+ the scoped definition-authoring / git-protocol docs). Every entry is a PATH the
    agent reads IN PLACE — never an inlined body (role-as-documents, H40). Two different levels get
    DIFFERENT per-level docs, so the manifest is genuinely role_variant-selected.
    """
    role_variant = str(getattr(level_config, "role_variant", None) or "")
    if role_variant.startswith("plan-alignment#"):
        role = role_variant.split("#", 1)[1]
        instruction_by_role = {
            "reconstruction-verification": (
                "operational/plan-alignment/reconstruction-window.md"
            ),
            "reconstruction-construction": (
                "operational/plan-alignment/reconstruction-window.md"
            ),
            "comparator": "operational/plan-alignment/adversarial-comparator.md",
            "coherence": "operational/plan-alignment/coherence.md",
            "atomization": "operational/plan-alignment/atomization.md",
        }
        instruction = instruction_by_role.get(role)
        if instruction is None:
            return []
        true_level = str(getattr(level_config, "level", None) or "L2+")
        # The ordinary L2+ identity remains auto-loaded by the runtime adapter.  The bounded
        # semantic instruction is additive and the exact-read sandbox prevents that identity
        # from expanding the seat's evidence window.
        return [
            *identity_docs(true_level),
            instruction,
            "operational/shared/comms-protocol.md",
        ]
    if role_variant.startswith("product-probe#"):
        role = role_variant.split("#", 1)[1]
        instruction_by_role = {
            "user-simulation": "operational/review-checks/user-simulation.md",
            "performance-robustness": (
                "operational/review-checks/performance-robustness.md"
            ),
        }
        instruction = instruction_by_role.get(role)
        if instruction is None:
            return []
        true_level = str(getattr(level_config, "level", None) or "L2+")
        return [
            *identity_docs(true_level),
            instruction,
            "operational/shared/comms-protocol.md",
            "operational/shared/templates/check-review-report-template.md",
        ]

    level = _level_token(level_config)

    manifest: list[str] = []

    # --- Per-level role docs (the seat's own identity/boundaries/self-monitoring) ---
    manifest.extend(identity_docs(level))
    # Per-level extras (handbook / planning-template / swe-handbook, …).
    manifest.extend(_LEVEL_EXTRAS.get(level, ()))

    # --- Shared, always-loaded contract docs ---
    manifest.extend(_SHARED_ALWAYS)
    # agent-definition-principles.md: definition-authoring levels (L1–L4) only; L5 and review seats omit it.
    if level in ("L1", "L2", "L3", "L4"):
        manifest.append(_DEFINITION_AUTHORING_DOC)
    # git-protocol.md: levels that own code movement and gate evidence.
    # (it runs the suites itself — the independent testing pass).
    if level in ("L3", "L4", "L5", "L5+"):
        manifest.append(_GIT_PROTOCOL_DOC)

    return manifest


# ---------------------------------------------------------------------------
# Result dataclasses (FORK-BRIEF-SHAPE — see module docstring).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NeutralContract:
    """The runtime-NEUTRAL task contract + the per-spawn load-manifest (§6.3 / ROLE-RESOLUTION §2).

    IDENTICAL across runtimes — the adapter injects only the three runtime-specific things over this
    neutral core. ``load_manifest`` is the "Identity — Load These Documents" list of harness-root
    PATHS the agent reads in place (role-as-documents); the role is NOT inlined into any field here.
    """

    node_address: str
    level: str
    role_variant: str
    system_prompt_file: str
    load_manifest: list[str]
    spec_pointer: Optional[str]
    frozen_acceptance_ref: Optional[str]
    workspace: Optional[str]
    status_md: Optional[str] = None
    log_md: Optional[str] = None
    report_md: Optional[str] = None
    # The RESOLVED §2.5a containment block the chokepoint attaches (dataclasses.replace) when a spawn
    # REQUESTS containment — None for the unjailed/structural (dry-run) shape. When present the adapter
    # renders the §2.3 seatbelt profile + wraps the pane with sandbox-exec (§7.1); when None the pane is
    # the bare ``env -i`` isolator. The seat the JAIL-WIRING attaches the resolved block onto.
    containment_profile: Optional[dict] = None
    reporting: str = (
        "Report via the durable work node: write status.md / log.md and the final report.md; "
        "sign off by writing .signal.<seat>.json {signal: DONE|FAILED, ts, owner_token, "
        "evidence} into your node dir (atomic tmp+rename; copy owner_token verbatim from "
        ".sign-off.<seat>.json in the same dir) — see operational/shared/comms-protocol.md, "
        "Terminal Signal. If blocked, submit a canonical direct-edge message to your parent with "
        "needs_answer: true and park without writing a terminal signal."
    )


@dataclass(frozen=True)
class DeltaBrief:
    """The resume DELTA brief (§6.4) — what CHANGED since the prior incarnation, NOT the full brief.

    Points at the durable work node the fresh instance re-reads (status/log/report/frozen
    acceptance). Carries the prior incarnation's identity (so the new instance knows what it is
    succeeding) and the explicit ``changes`` (parent answers, new messages, reconcile findings).
    """

    node_address: str
    prior_incarnation: dict
    changes: dict
    workspace: Optional[str]
    status_md: Optional[str]
    log_md: Optional[str]
    report_md: Optional[str]
    frozen_acceptance_ref: Optional[str]
    delta: str


# ---------------------------------------------------------------------------
# assemble_neutral — the runtime-NEUTRAL contract + the role_variant-selected load-manifest.
# ---------------------------------------------------------------------------

def assemble_neutral(node_address: str, level_config, work_node: dict) -> NeutralContract:
    """Assemble the runtime-NEUTRAL task contract + the per-spawn load-manifest (§6.1 STEP2 / §6.3).

    Writes the "Identity — Load These Documents" load-manifest (selected by ``role_variant``) into
    the contract as a list of harness-root PATHS the agent reads IN PLACE (role-as-documents, H40) —
    the role is NEVER flattened into the contract text. Carries the node identity/address, the spec
    pointer, the FROZEN-ACCEPTANCE reference, the workspace, and the durable work-node pointers — all
    IDENTICAL across runtimes (the adapter injects only the three runtime-specific things over this).
    """
    work_node = work_node or {}
    level = getattr(level_config, "level", None) or _level_token(level_config)
    role_variant = getattr(level_config, "role_variant", None) or level

    return NeutralContract(
        node_address=node_address,
        level=level,
        role_variant=role_variant,
        system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
        load_manifest=_assemble_load_manifest(level_config),
        spec_pointer=work_node.get("spec_pointer"),
        frozen_acceptance_ref=work_node.get("frozen_acceptance_ref"),
        workspace=work_node.get("workspace"),
        status_md=work_node.get("status_md"),
        log_md=work_node.get("log_md"),
        report_md=work_node.get("report_md"),
    )


# ---------------------------------------------------------------------------
# delta_brief — the resume DELTA (§6.4): what changed, pointing at the durable work node.
# ---------------------------------------------------------------------------

def delta_brief(
    node_address: str,
    prior_incarnation: dict,
    work_node: dict,
    changes: dict,
) -> DeltaBrief:
    """Assemble the resume DELTA brief (§6.4) — NOT the full original brief.

    NAMES what changed since the prior incarnation (parent answers to an ESCALATED, new messages,
    reconcile findings — the ``changes`` dict) and points at the durable work node the fresh instance
    RE-READS (status.md / log.md / report.md / frozen acceptance). A delta, not the whole brief: the
    fresh instance re-reads the durable node for the unchanged context and only consumes the delta for
    what moved.
    """
    work_node = work_node or {}
    changes = changes or {}
    prior_incarnation = prior_incarnation or {}

    # A human-readable summary of WHAT CHANGED — names the change keys so the delta is a delta.
    if changes:
        delta_summary = "Changes since the prior incarnation: " + "; ".join(
            f"{key}={value!r}" for key, value in changes.items()
        )
    else:
        delta_summary = (
            "No itemized changes supplied; re-read the durable work node "
            "(status.md / log.md / report.md) for current state."
        )

    return DeltaBrief(
        node_address=node_address,
        prior_incarnation=dict(prior_incarnation),
        changes=dict(changes),
        workspace=work_node.get("workspace"),
        status_md=work_node.get("status_md"),
        log_md=work_node.get("log_md"),
        report_md=work_node.get("report_md"),
        frozen_acceptance_ref=work_node.get("frozen_acceptance_ref"),
        delta=delta_summary,
    )
