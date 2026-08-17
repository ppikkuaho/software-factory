"""Doc-system checker — the shared-blocks registry walked red/green (design/DOC-SYSTEM.md).

WHAT THIS INSTRUMENT IS: the deterministic, no-model drift/omission catcher for the per-level
agent documentation. Cross-level duties live ONCE under ``operational/shared/blocks/`` (versioned
content blocks, plus named per-level adaptations); per-level role docs carry RENDERED COPIES
between HTML-comment markers (``<!-- block:<id> v<N> -->`` … ``<!-- /block:<id> -->``); the
registry (``operational/shared/blocks/registry.json``) says which doc carries which block at which
version, verbatim or adapted. This suite walks the registry via ``tools/render_blocks.py`` and
fails RED on: a missing block in a registered carrier, a stale marker version, content drift
inside markers, markers the registry doesn't know, leftover legacy markers, and missing
sources/templates. Everything OUTSIDE markers is the level's own craft — never touched, never
checked here.

THE CRITICAL CONSTRAINT (user ruling — restated from the tool's docstring, do not weaken):
**a green run means MECHANICAL CONFORMANCE ONLY** — no drift/omission accidents. It does NOT mean
the documentation is healthy or behaviorally effective; tests cannot encode the desired behavior.
Documentation HEALTH belongs to the judgment layer: periodic doc review and, above all, behavioral
evidence from live runs (the RUN-ADHERENCE-AUDIT instrument — Run-2's defect distribution, seats
bouncing off the E2 gate despite conformant prose, WAS doc-health data). Do not read a green
checker as "the docs work".

Authoritative sources (grounded, not recalled):
  * design/DOC-SYSTEM.md — the mechanism, the decisions, the constraint.
  * working-notes/splice_gate_contracts.py — the prior art this generalizes (anchor-splice,
    idempotent, abort-on-missing-anchor).
  * harnessd/return_contract.py — the E2 runtime floor the report-contract block describes
    (this suite never touches it; the floor is runtime, this checker is repo hygiene).
  * LIVE-RUN-2026-06-11-FINDINGS.md — the Run-2 coverage accidents (L3's ZERO report-duty
    mentions vs L5's three) these blocks close.

BIAS TO REAL (the pieces-present pattern): the green test runs against the REAL operational/
corpus in place. The catch-tests run against a deliberately-broken TEMP COPY of the real corpus
(a removed block, drifted content, a stale version, an unknown marker, a deleted template) and
assert each defect is caught AND NAMED — a wrong checker that silently passes a mutant is itself
caught here.

LOAD-BEARING PROPERTIES (each pins a regression a careless edit would reintroduce):
  * the real corpus is GREEN — every registered carrier carries its block, current, undrifted;
  * the legacy LR-13 splices are MIGRATED into the registry (gate-output-contract covers
    L1/L2/L3/L4 + L5+; the old marker string no longer appears anywhere);
  * the L3 report gap is CLOSED (L3 carries report-contract — the Run-2 MISSING-REPORT bounce);
  * rendering is IDEMPOTENT and never touches content outside markers;
  * every defect class fails RED with a defect that NAMES doc + block.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools import render_blocks as rb

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_REL = "operational/shared/blocks/registry.json"

ALL_SIX_ROLE_DOCS = {
    "operational/L1/role.md",
    "operational/L2/role.md",
    "operational/L3/role.md",
    "operational/L4/role.md",
    "operational/L5/role.md",
    "operational/L5+/role.md",
}

ALL_PILOT_BEHAVIOR_CARRIERS = {
    "operational/L1/role.md",
    "operational/L2/role.md",
    "operational/L2+/role.md",
    "operational/L3/role.md",
    "operational/L3+/role.md",
    "operational/L4/role.md",
    "operational/L4+/role.md",
    "operational/L5/role.md",
    "operational/L5+/role.md",
    "operational/shared/review-handbook.md",
}


def _registry(root: Path) -> dict:
    return json.loads((root / REGISTRY_REL).read_text(encoding="utf-8"))


def test_behavioral_law_blocks_have_single_sources_and_exact_role_carriers():
    registry = _registry(REPO_ROOT)

    signoff = registry["blocks"]["decision-delivery-signoff"]
    assert signoff["source"] == "decision-delivery-signoff.md"
    assert signoff["version"] == 5  # v5 = the seat-side substrate fail-fast law
    assert {carrier["doc"] for carrier in signoff["carriers"]} == ALL_PILOT_BEHAVIOR_CARRIERS
    assert {carrier["variant"] for carrier in signoff["carriers"]} == {None}

    review = registry["blocks"]["review-accountability"]
    assert review["source"] == "review-accountability.md"
    assert {
        (carrier["doc"], carrier["variant"])
        for carrier in review["carriers"]
    } == {
        ("operational/L2+/role.md", None),
        ("operational/L3+/role.md", None),
        ("operational/L4+/role.md", None),
        ("operational/L5+/role.md", "review-accountability.reviewer.md"),
        (
            "operational/shared/review-handbook.md",
            "review-accountability.reviewer.md",
        ),
    }


@pytest.fixture()
def corpus_copy(tmp_path: Path) -> Path:
    """A mutable copy of the REAL doc corpus (operational/ + tools' inputs) for the catch-tests.

    Only ``operational/`` is copied — the registry, blocks, templates, and carrier docs all live
    under it, and the checker resolves every path relative to the root it is handed.
    """
    root = tmp_path / "corpus"
    shutil.copytree(REPO_ROOT / "operational", root / "operational")
    return root


# ---------------------------------------------------------------------------
# The suite gate: the REAL corpus is green.
# ---------------------------------------------------------------------------

class TestRealCorpusGreen:
    def test_real_corpus_has_no_defects(self):
        defects = rb.check(REPO_ROOT)
        assert defects == [], "doc-system defects on the real corpus:\n" + "\n".join(
            d.message for d in defects
        )

    def test_render_on_real_corpus_is_a_noop(self, tmp_path):
        """Idempotency on the landed state: rendering the (already-rendered) corpus changes nothing."""
        root = tmp_path / "corpus"
        shutil.copytree(REPO_ROOT / "operational", root / "operational")
        before = {p: p.read_bytes() for p in sorted((root / "operational").rglob("*.md"))}
        actions = rb.render(root)
        assert actions == []
        after = {p: p.read_bytes() for p in sorted((root / "operational").rglob("*.md"))}
        assert before == after


# ---------------------------------------------------------------------------
# Registry coverage pins — the settled landings cannot silently regress.
# ---------------------------------------------------------------------------

class TestRegistryCoverage:
    def test_build_to_claim_duties_cover_the_decision_surfaces(self):
        reg = _registry(REPO_ROOT)
        expected = {
            "claim-proof-account": {
                "operational/L4/role.md",
                "operational/L5/role.md",
                "operational/L5/swe-handbook.md",
            },
            "acceptance-package-proof": {
                "operational/L4/role.md",
                "operational/L5/role.md",
                "operational/L5/config.md",
                "operational/shared/templates/test-author-brief-template.md",
            },
            "review-by-driving": {
                "operational/L5+/role.md",
                "operational/shared/review-handbook.md",
                "operational/L4+/role.md",
                "operational/L3+/role.md",
                "operational/L2+/role.md",
            },
            "composition-proof": {
                "operational/shared/review-handbook.md",
                "operational/L4+/role.md",
                "operational/L3+/role.md",
                "operational/L2+/role.md",
            },
        }
        for block, docs in expected.items():
            carriers = {row["doc"] for row in reg["blocks"][block]["carriers"]}
            assert carriers == docs, block

        package = rb.landed_block_body(
            REPO_ROOT,
            "operational/shared/templates/test-author-brief-template.md",
            "acceptance-package-proof",
        )
        assert "tests/live-scenario.md" in package
        assert "tests/red-run-log.md" in package
        l5_review = rb.landed_block_body(
            REPO_ROOT,
            "operational/L5+/role.md",
            "review-by-driving",
        )
        assert "each new check" in l5_review
        assert "observed RED" in l5_review
        composition = rb.landed_block_body(
            REPO_ROOT,
            "operational/L3+/role.md",
            "composition-proof",
        )
        assert "negative and must-never-fail" in composition

    def test_report_contract_covers_all_six_levels(self):
        reg = _registry(REPO_ROOT)
        carriers = {c["doc"] for c in reg["blocks"]["report-contract"]["carriers"]}
        assert carriers == ALL_SIX_ROLE_DOCS

    def test_l3_report_gap_is_closed(self):
        """The Run-2 finding: L3's role doc had ZERO report.md mentions; both live L3s were
        bounced by E2 (MISSING-REPORT). The block landing closes it — and keeps it closed."""
        text = (REPO_ROOT / "operational/L3/role.md").read_text(encoding="utf-8")
        assert "report.md" in text
        assert "<!-- block:report-contract v" in text

    def test_plan_first_and_trace_discipline_cover_all_six(self):
        reg = _registry(REPO_ROOT)
        for block in ("plan-first", "trace-discipline"):
            carriers = {c["doc"] for c in reg["blocks"][block]["carriers"]}
            assert carriers == ALL_SIX_ROLE_DOCS, block

    def test_legacy_gate_splices_migrated_not_lost(self):
        """The LR-13 gate-output contracts are ADOPTED INTO the registry: the per-level artifact
        names survive verbatim in the carriers, and the legacy marker string is gone everywhere."""
        reg = _registry(REPO_ROOT)
        carriers = {c["doc"]: c for c in reg["blocks"]["gate-output-contract"]["carriers"]}
        assert set(carriers) == {
            "operational/L1/role.md",
            "operational/L2/role.md",
            "operational/L3/role.md",
            "operational/L4/role.md",
            "operational/L5+/role.md",
        }
        expectations = {
            "operational/L1/role.md": "fidelity-judgment.md",
            "operational/L2/role.md": "composition-review.md",
            "operational/L3/role.md": "area-composition-review.md",
            "operational/L4/role.md": "composition-report.md",
            "operational/L5+/role.md": "verdict table",
        }
        for doc, needle in expectations.items():
            body = rb.landed_block_body(REPO_ROOT, doc, "gate-output-contract")
            assert needle in body, f"{doc}: gate artifact name {needle!r} lost in migration"
        # The L2 carrier now records the 2026-06-16 ruling: no zero-L3 product path.
        l2_gate = rb.landed_block_body(
            REPO_ROOT, "operational/L2/role.md", "gate-output-contract"
        )
        assert "L3 Execution Spine (non-collapsible)" in l2_gate
        assert "does not spawn L4 directly" in l2_gate
        assert "may skip the L3 layer" not in l2_gate
        for md in (REPO_ROOT / "operational").rglob("*.md"):
            assert rb.LEGACY_MARKER not in md.read_text(encoding="utf-8"), md

    def test_templates_exist_and_carry_the_settled_shape(self):
        report_t = (REPO_ROOT / "operational/shared/templates/report-template.md").read_text(
            encoding="utf-8"
        )
        for needle in (
            "**From:**",
            "**Status:**",
            "## Outcome",
            "## Requirement IDs discharged",
            "## Verification evidence",
            "## Deviations & concerns",
            "## Sign-off checklist",
            "Every item in the durable file `plan.md` is checked",
            "No ancestor/project `log.md` was edited",
        ):
            assert needle in report_t, needle
        assert "Project log appended" not in report_t
        plan_t = (REPO_ROOT / "operational/shared/templates/plan-template.md").read_text(
            encoding="utf-8"
        )
        for needle in ("**Goal:**", "report-template.md", "Sign off"):
            assert needle in plan_t, needle

    def test_l5plus_template_adaptation_is_registered_and_reasoned(self):
        """The first exercise of the adaptation mechanism at the template tier: a reviewer does
        not DISCHARGE requirements — it VERIFIES them (QUALITY-GATE M52: the per-criterion verdict
        table is the reviewer's gate artifact). Registered with base + reason, never a fork."""
        reg = _registry(REPO_ROOT)
        adaptations = [e for e in reg["templates"] if isinstance(e, dict)]
        l5p = [e for e in adaptations if e.get("level") == "L5+"]
        assert len(l5p) == 1
        entry = l5p[0]
        assert entry["adapts"] == "operational/shared/templates/report-template.md"
        assert entry.get("reason"), "an unreasoned adaptation is a fork"
        text = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
        assert "## Requirement IDs verified (per-criterion verdicts)" in text
        assert "## Requirement IDs discharged" not in text
        assert "No ancestor/project `log.md` was edited" in text
        assert "Project log appended" not in text

    def test_l5_template_adaptation_carries_the_claim_account(self):
        reg = _registry(REPO_ROOT)
        adaptations = [e for e in reg["templates"] if isinstance(e, dict)]
        l5 = [e for e in adaptations if e.get("level") == "L5"]
        assert len(l5) == 1
        entry = l5[0]
        assert entry["adapts"] == "operational/shared/templates/report-template.md"
        assert entry.get("reason")
        text = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
        for heading in (
            "## Drove and Watched",
            "## Inferred",
            "## Residual Uncertainty",
            "## Inventory",
        ):
            assert heading in text

    def test_l5plus_report_contract_is_a_named_adaptation_with_the_citation_duty(self):
        """Run-2: both L5+ reviewers tripped the E2 citation check — the L5+ bundle carried no
        citation duty for the reviewer's OWN report. The L5+ landing of report-contract is a
        registered variant (verifies-not-discharges) that closes it."""
        reg = _registry(REPO_ROOT)
        carriers = {c["doc"]: c for c in reg["blocks"]["report-contract"]["carriers"]}
        assert carriers["operational/L5+/role.md"]["variant"] == "report-contract.L5+.md"
        body = rb.landed_block_body(REPO_ROOT, "operational/L5+/role.md", "report-contract")
        assert "VERIFIED" in body
        assert "report-template.L5+.md" in body
        # The executor-side landings stay on the base (discharge framing).
        l5_body = rb.landed_block_body(REPO_ROOT, "operational/L5/role.md", "report-contract")
        assert "report-template.L5+.md" not in l5_body

    def test_trace_discipline_carries_declaration_ownership(self):
        """v2: declaration ownership follows artifact ownership — the law behind Run-2's DUP-ID
        bounces (parent briefs and child acceptance files declaring the same IDs)."""
        for doc in sorted(ALL_SIX_ROLE_DOCS):
            body = rb.landed_block_body(REPO_ROOT, doc, "trace-discipline")
            assert "Declaration ownership follows artifact ownership" in body, doc


# ---------------------------------------------------------------------------
# Catch-tests — every defect class fails RED, caught and NAMED, on a broken copy.
# ---------------------------------------------------------------------------

def _kinds(defects) -> set:
    return {d.kind for d in defects}


def _messages(defects) -> str:
    return "\n".join(d.message for d in defects)


class TestDefectsCaughtAndNamed:
    def test_missing_block_is_caught(self, corpus_copy):
        """A registered carrier whose marker region is deleted -> MISSING-BLOCK naming doc+block."""
        doc = corpus_copy / "operational/L3/role.md"
        text = doc.read_text(encoding="utf-8")
        start = text.index("<!-- block:report-contract")
        end = text.index("<!-- /block:report-contract -->") + len("<!-- /block:report-contract -->")
        doc.write_text(text[:start] + text[end:], encoding="utf-8")
        defects = rb.check(corpus_copy)
        assert "MISSING-BLOCK" in _kinds(defects)
        assert any(
            d.kind == "MISSING-BLOCK"
            and d.block == "report-contract"
            and "operational/L3/role.md" in d.message
            for d in defects
        ), _messages(defects)

    def test_content_drift_inside_markers_is_caught(self, corpus_copy):
        doc = corpus_copy / "operational/L5/role.md"
        text = doc.read_text(encoding="utf-8")
        doc.write_text(
            text.replace("required at DONE at EVERY level", "optional at DONE", 1),
            encoding="utf-8",
        )
        defects = rb.check(corpus_copy)
        assert any(
            d.kind == "CONTENT-DRIFT"
            and d.block == "report-contract"
            and "operational/L5/role.md" in d.message
            for d in defects
        ), _messages(defects)

    def test_stale_version_is_caught(self, corpus_copy):
        """Source bumped in the registry without a re-render -> STALE-VERSION (before any diff)."""
        reg_path = corpus_copy / REGISTRY_REL
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        reg["blocks"]["trace-discipline"]["version"] = 9
        reg_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
        defects = rb.check(corpus_copy)
        stale = [d for d in defects if d.kind == "STALE-VERSION" and d.block == "trace-discipline"]
        assert len(stale) == 6, _messages(defects)  # all six carriers are stale at once

    def test_unknown_marker_is_caught(self, corpus_copy):
        doc = corpus_copy / "operational/L4/role.md"
        text = doc.read_text(encoding="utf-8")
        doc.write_text(
            text
            + "\n<!-- block:freelance-duty v1 -->\nan unregistered landing\n<!-- /block:freelance-duty -->\n",
            encoding="utf-8",
        )
        defects = rb.check(corpus_copy)
        assert any(
            d.kind == "UNKNOWN-MARKER" and d.block == "freelance-duty" for d in defects
        ), _messages(defects)

    def test_legacy_marker_is_caught(self, corpus_copy):
        doc = corpus_copy / "operational/L2/role.md"
        doc.write_text(
            doc.read_text(encoding="utf-8") + "\n" + rb.LEGACY_MARKER + "\n", encoding="utf-8"
        )
        defects = rb.check(corpus_copy)
        assert "LEGACY-MARKER" in _kinds(defects), _messages(defects)

    def test_missing_template_is_caught(self, corpus_copy):
        (corpus_copy / "operational/shared/templates/report-template.md").unlink()
        defects = rb.check(corpus_copy)
        assert any(
            d.kind == "MISSING-TEMPLATE" and "report-template.md" in d.message for d in defects
        ), _messages(defects)

    def test_unreasoned_template_adaptation_is_caught(self, corpus_copy):
        """An adaptation that states no reason is a fork — BAD-REGISTRY."""
        reg_path = corpus_copy / REGISTRY_REL
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        for entry in reg["templates"]:
            if isinstance(entry, dict):
                entry.pop("reason", None)
        reg_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
        defects = rb.check(corpus_copy)
        assert any(
            d.kind == "BAD-REGISTRY" and "no reason" in d.message for d in defects
        ), _messages(defects)

    def test_missing_source_is_caught(self, corpus_copy):
        (corpus_copy / "operational/shared/blocks/plan-first.md").unlink()
        defects = rb.check(corpus_copy)
        assert any(
            d.kind == "MISSING-SOURCE" and d.block == "plan-first" for d in defects
        ), _messages(defects)

    def test_unclosed_marker_is_caught(self, corpus_copy):
        doc = corpus_copy / "operational/L5+/role.md"
        text = doc.read_text(encoding="utf-8")
        doc.write_text(text.replace("<!-- /block:plan-first -->", "", 1), encoding="utf-8")
        defects = rb.check(corpus_copy)
        assert any(
            d.kind in ("UNCLOSED-MARKER", "MISSING-BLOCK") and d.block == "plan-first"
            for d in defects
        ), _messages(defects)


# ---------------------------------------------------------------------------
# The render tool: heals drift, lands missing blocks at the anchor, touches NOTHING else.
# ---------------------------------------------------------------------------

class TestRender:
    def test_render_heals_drift_then_green(self, corpus_copy):
        doc = corpus_copy / "operational/L5/role.md"
        text = doc.read_text(encoding="utf-8")
        doc.write_text(
            text.replace("required at DONE at EVERY level", "optional at DONE", 1),
            encoding="utf-8",
        )
        actions = rb.render(corpus_copy)
        assert actions, "render reported nothing despite a drifted block"
        assert rb.check(corpus_copy) == []

    def test_render_lands_a_removed_block_at_the_anchor(self, corpus_copy):
        doc = corpus_copy / "operational/L3/role.md"
        text = doc.read_text(encoding="utf-8")
        start = text.index("<!-- block:report-contract")
        end = text.index("<!-- /block:report-contract -->") + len("<!-- /block:report-contract -->")
        doc.write_text(text[:start] + text[end:], encoding="utf-8")
        rb.render(corpus_copy)
        healed = doc.read_text(encoding="utf-8")
        assert "<!-- block:report-contract v" in healed
        assert healed.index("<!-- block:report-contract") < healed.index(
            "## Visibility Scope (F34)"
        )
        assert rb.check(corpus_copy) == []

    def test_render_never_touches_outside_markers(self, corpus_copy):
        """The level's own craft is untouchable: sentinel prose outside markers survives a
        render that heals drift inside them."""
        doc = corpus_copy / "operational/L5/role.md"
        sentinel = "SENTINEL-CRAFT-PROSE-9f31 — the level's own, not the tool's.\n\n"
        text = doc.read_text(encoding="utf-8")
        text = sentinel + text.replace("required at DONE at EVERY level", "optional at DONE", 1)
        doc.write_text(text + "\nTRAILING-SENTINEL-9f31\n", encoding="utf-8")
        rb.render(corpus_copy)
        healed = doc.read_text(encoding="utf-8")
        assert healed.startswith(sentinel)
        assert healed.rstrip().endswith("TRAILING-SENTINEL-9f31")
        assert rb.check(corpus_copy) == []

    def test_render_refuses_a_missing_anchor_loudly(self, corpus_copy):
        """A block that is neither landed nor ancorable must surface, never silently skip
        (the splice prior-art's no-partial-landing rule)."""
        doc = corpus_copy / "operational/L3/role.md"
        text = doc.read_text(encoding="utf-8")
        start = text.index("<!-- block:report-contract")
        end = text.index("<!-- /block:report-contract -->") + len("<!-- /block:report-contract -->")
        text = text[:start] + text[end:]
        doc.write_text(text.replace("## Visibility Scope (F34)", "## Renamed Scope"), encoding="utf-8")
        with pytest.raises(rb.RenderError, match="anchor"):
            rb.render(corpus_copy)
