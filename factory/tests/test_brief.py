"""Increment 10 — FROZEN acceptance for brief.py (the runtime-NEUTRAL contract + load-manifest,
and the resume DELTA brief). Tests ONLY — NO implementation. RED first.

Authoritative sources (grounded, not recalled — Lesson 4):
  * IMPLEMENTATION-PLAN §2.11 — brief.py FROZEN interface:
        assemble_neutral(node_address, level_config, work_node) -> NeutralContract
        delta_brief(node_address, prior_incarnation, work_node, changes) -> DeltaBrief
  * IMPLEMENTATION-PLAN — Increment-10 role-as-documents note (L770-777): assemble_neutral
    WRITES the per-spawn load-manifest ("Identity — Load These Documents") into the node,
    selected by role_variant — listing the role docs the agent READS in place (role-as-documents,
    H40). The role is NEVER flattened into the system prompt / argv.
  * DAEMON §6.1 STEP2 — the runtime-NEUTRAL brief INCLUDING its load-manifest (per-level role docs
    + always-loaded shared contract docs + referenced design docs), assembled by role_variant.
  * DAEMON §6.3 — the neutral task contract: identity/address, spec pointer, frozen acceptance ref,
    interface contracts, constraints, workspace location, reporting expectations — identical across
    runtimes (the adapter injects only the 3 runtime-specific things over THIS neutral core).
  * DAEMON §6.4 — the delta brief: what changed since the prior incarnation, pointing at the durable
    work node the fresh instance re-reads.

NO IMPLEMENTATION here — harnessd/spawn/brief.py does not exist yet (RED until written).

Load-bearing properties (each pins a mutant):
  * assemble_neutral writes a load-manifest selected by role_variant (mutant: a constant manifest
    ignoring role_variant -> two different levels get the same role docs -> caught).
  * the manifest lists role docs as PATHS the agent reads in place — the role is NOT inlined into
    the contract/argv text (role-as-documents, H40) (mutant: inline role.md text -> caught).
  * the neutral contract carries the node identity/address + a frozen-acceptance reference
    (mutant: drop the address / acceptance ref -> caught).
  * delta_brief points at the durable work node + names what changed (mutant: emit the full original
    brief instead of a delta -> caught).
"""

from __future__ import annotations

import importlib

import pytest

import harnessd.config as config


def _brief():
    return importlib.import_module("harnessd.spawn.brief")


# A minimal durable "work node" pointer the brief assembles against (status/log/report/acceptance).
WORK_NODE = {
    "node_address": "proj/widget#exec",
    "workspace": "/runtime/work/proj/widget#exec",
    "spec_pointer": "design/intent-spec.md",
    "frozen_acceptance_ref": "tests/frozen/acceptance-proj-widget.md",
    "status_md": "/runtime/work/proj/widget#exec/status.md",
    "log_md": "/runtime/work/proj/widget#exec/log.md",
    "report_md": "/runtime/work/proj/widget#exec/report.md",
}

NODE = "proj/widget#exec"


def _manifest_blob(contract) -> str:
    """Best-effort flatten of the assembled load-manifest to a searchable string.

    Accepts a structured NeutralContract (attribute or dict access) — pulls the load-manifest /
    full repr — so the test does not over-pin the dataclass field names while still asserting the
    role docs are present AS PATHS.
    """
    for attr in ("load_manifest", "manifest", "documents", "load_these_documents"):
        val = getattr(contract, attr, None)
        if val is None and isinstance(contract, dict):
            val = contract.get(attr)
        if val is not None:
            return repr(val)
    return repr(contract)


# ===========================================================================
# assemble_neutral — the load-manifest is selected by role_variant (H40 role-as-documents).
# Two different levels (different role_variant) must get DIFFERENT role docs.
#
# Mutant killed: a constant manifest that ignores role_variant -> L1 and L3 get identical docs.
# ===========================================================================

def test_assemble_neutral_manifest_selected_by_role_variant():
    brief = _brief()

    l1 = config.LevelConfig.for_level("L1")
    l3 = config.LevelConfig.for_level("L3")

    c1 = brief.assemble_neutral(NODE, l1, WORK_NODE)
    c3 = brief.assemble_neutral(NODE, l3, WORK_NODE)

    blob1 = _manifest_blob(c1)
    blob3 = _manifest_blob(c3)

    # The manifest is selected by role_variant: an L1 seat loads the L1 role docs, an L3 seat the L3.
    assert "L1" in blob1, "the L1 load-manifest must name the L1 role docs (selected by role_variant)"
    assert "L3" in blob3, "the L3 load-manifest must name the L3 role docs (selected by role_variant)"
    assert blob1 != blob3, (
        "the load-manifest must be SELECTED by role_variant — two different levels must get "
        "different role docs (mutant: a constant manifest ignoring role_variant -> caught)"
    )


@pytest.mark.parametrize(
    ("level", "role_doc", "template"),
    [
        ("L4+", "operational/L4+/role.md", "operational/shared/templates/workstream-composition-review-template.L4+.md"),
        ("L3+", "operational/L3+/role.md", "operational/shared/templates/area-composition-review-template.L3+.md"),
        ("L2+", "operational/L2+/role.md", "operational/shared/templates/product-composition-review-template.L2+.md"),
    ],
)
def test_higher_review_seats_load_review_docs_not_executor_docs(level, role_doc, template):
    """A higher #review seat must boot into the review bundle, not the producer bundle.

    Mutant killed: resolving L4#review/L3#review/L2#review through the bare executor level causes
    the spawned review seat to read executor docs and miss the gate-specific prompt/template.
    """
    brief = _brief()
    lc = config.LevelConfig.for_level(level)
    contract = brief.assemble_neutral(NODE, lc, WORK_NODE)
    blob = _manifest_blob(contract)

    assert role_doc in blob, f"{level} must load its review role doc"
    assert template in blob, f"{level} must load its level-specific review template"
    assert "operational/shared/review-handbook.md" in blob
    assert "operational/shared/templates/higher-gate-review-plan-template.md" in blob
    assert "operational/shared/templates/check-review-report-template.md" in blob
    assert "design/GATE-LIFECYCLE.md" in blob
    assert "design/HIGHER-LEVEL-GATES.md" in blob


def test_assemble_neutral_role_is_documents_not_inlined():
    """The role arrives as DOCUMENTS the agent reads in place (paths under the harness root) — it is
    NOT flattened into the contract/prompt text (role-as-documents, H40).

    Mutant killed: inline the role.md prose into the contract -> the role becomes prompt content.
    """
    brief = _brief()
    l3 = config.LevelConfig.for_level("L3")
    contract = brief.assemble_neutral(NODE, l3, WORK_NODE)
    blob = _manifest_blob(contract)

    # The role docs are referenced AS PATHS (the per-level operational/L{n}/{soul,role,config}.md +
    # the always-loaded operational/shared/* contract docs) — file references, not inlined bodies.
    assert "operational/" in blob and ".md" in blob, (
        "the load-manifest must list role docs AS PATHS the agent reads in place (operational/L*/*.md "
        "+ operational/shared/*.md) — role-as-documents (H40), not inlined prose"
    )


def test_assemble_neutral_carries_identity_and_frozen_acceptance():
    """The runtime-NEUTRAL contract carries the node identity/address + the frozen-acceptance
    reference + the spec pointer (§6.3). These are identical across runtimes; the adapter injects
    only the 3 runtime-specific things over THIS neutral core.

    Mutant killed: drop the address or the frozen-acceptance ref -> the child cannot anchor its work.
    """
    brief = _brief()
    l3 = config.LevelConfig.for_level("L3")
    contract = brief.assemble_neutral(NODE, l3, WORK_NODE)
    blob = repr(contract)

    assert NODE in blob, "the neutral contract must carry the node identity/address (§6.3)"
    assert "acceptance" in blob.lower(), (
        "the neutral contract must carry the FROZEN ACCEPTANCE reference (§6.3) — the child's "
        "spec-anchored target"
    )


def test_neutral_reporting_teaches_only_done_failed_terminal_signals():
    brief = _brief()
    contract = brief.assemble_neutral(
        NODE,
        config.LevelConfig.for_level("L3"),
        WORK_NODE,
    )

    assert "DONE|FAILED" in contract.reporting
    assert "ESCALATED" not in contract.reporting
    assert "needs_answer" in contract.reporting
    assert "park" in contract.reporting


# ===========================================================================
# delta_brief — a DELTA (what changed since the prior incarnation), NOT the full original brief,
# pointing at the durable work node the fresh instance re-reads (§6.4).
#
# Mutant killed: emit the full original brief on resume -> the delta carries no 'what changed'.
# ===========================================================================

def test_delta_brief_names_changes_and_points_at_work_node():
    brief = _brief()

    prior_incarnation = {
        "session_uuid": "sess-uuid-prior",
        "lease_epoch": 2,
        "ended_reason": "escalated",
    }
    changes = {
        "parent_answer": "use the v2 schema",
        "new_messages": 1,
        "reconcile_findings": "binding re-adopted",
    }
    delta = brief.delta_brief(NODE, prior_incarnation, WORK_NODE, changes)
    blob = repr(delta)

    # The delta NAMES what changed since the prior incarnation.
    assert "use the v2 schema" in blob or "parent_answer" in blob, (
        "delta_brief must NAME what changed since the prior incarnation (the parent's answer / new "
        "messages / reconcile findings) — a delta, not the full original brief"
    )
    # It points at the durable work node the fresh instance re-reads (status/log/report).
    assert NODE in blob, "delta_brief must point at the durable work node (the node address)"
