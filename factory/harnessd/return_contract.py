"""return_contract — E2: the deterministic return-contract walker on the sign-off path.

The 2026-06-11 live run + corpus read (LR-14) found the role docs' load-bearing enforcement —
"the return-contract hook walks your artifact and REJECTS it: you cannot report complete"
(operational/L1/role.md, operational/L2/role.md, operational/shared/intent-spec-contract.md) —
existed only as OFFLINE eval scoring (tools/eval_*.py). This module is the runtime half: invoked
by ``watchdog.check_terminal_signal`` on a fenced DONE signal, BEFORE ``chokepoint.collapse``.
A node whose return artifacts fail the deterministic floor is NOT collapsed: the signal stays on
disk, ONE edge-triggered typed-defect row lands in the run-ledger, and ONE defect line lands in
the node's inbox (the ③-wake delivers the nudge) so the agent fixes and re-signals.

THE v1 FLOOR (deterministic only — judgment stays eval-side, per the user-ratified split):
  1. MISSING-REPORT      — ``report.md`` must exist (non-empty) in the node dir at DONE. Every
                           level's role doc names the report as the parent-facing deliverable.
  1b. MISSING-GATE-ARTIFACT / MISSING-GATE-VERDICT — review seats must produce their level's
                           explicit gate artifact, and that artifact must carry
                           ``VERDICT: ACCEPT``, ``VERDICT: BOUNCE``, or ``VERDICT: ESCALATE``.
                           For L5+ the artifact is ``report.md``; for L4/L3/L2 it is the
                           portfolio gate artifact.
  1c. MISSING-RED-RUN-LOG — an L5 ``test_author`` must return a non-empty
                           ``tests/red-run-log.md``; L5+ judges whether every new check was
                           genuinely watched failing.
      MISSING-CLAIM-ACCOUNT-SECTIONS — an implementation L5 report must carry the four exact
                           account headings for drove-and-watched evidence, inference, residual
                           uncertainty, and parent-owned inventory; L5+ judges their substance.
  2. MISSING-REQUIREMENT-CITATION — (L5-class seats only, and ONLY when the node was GIVEN minted
                           IDs): the report must cite >=1 of the requirement IDs present in the
                           node's brief.md/acceptance.md ("you do not mint IDs — they are given to
                           you in the brief; you cite them", operational/L5/role.md §Outputs). A
                           node given NO IDs (a smoke project with no minted spine) owes none.
  3. MALFORMED-TRACE / DUP-ID / TRACE-CONTRADICTION — every ``<!-- trace: {...} -->`` stanza in
                           the node's own visible top-level *.md return artifacts must parse with
                           EXACTLY the closed field set {id, serves, kind, level, node}
                           (PLAN-ALIGNMENT-GATE §Requirements-Traceability), ids unique within
                           the node, and a ``kind: requirement`` dotted id must truncate INTO one
                           of its declared ``serves`` ids. Stanzas are validated WHERE PRESENT —
                           their existence is the gate/eval layer's question, not this floor's.

NEVER TRAP: FAILED and ESCALATED signals are exempt — an agent can always fail/escalate loud
without contract checks (a failing agent must never be wedged into its own refusal loop).

Edge-triggered: the defect row + inbox line land ONCE per harness-observed signal artifact
identity; a re-poll of the same still-failing artifact journals nothing, while a fresh artifact is
a new refusal edge even if the agent reused its authored ``ts``. A refused signal identity remains
refused even if the agent edits artifacts underneath it; repair is admitted only after the agent
rewrites the signal file and gives the harness a fresh artifact identity. Candidate artifact drift is
the exception: the candidate identity itself is invalid, so the reviewer is told to stop and wait for
a fresh candidate or explicit retry rather than to re-sign the same verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from harnessd import (
    addressing,
    clock,
    executor,
    ledger,
    plan_alignment_cell,
    review_dispatch,
    traceability,
)

# The closed trace-block field set (PLAN-ALIGNMENT-GATE §Requirements-Traceability).
_TRACE_FIELDS = traceability.TRACE_FIELDS
_TRACE_NON_RETURN_FILES = {"brief.md", "acceptance.md", "log.md"}
# Legacy daemon aggregates are NEVER return evidence. The pass-8 renderer removed their write
# path, but historical/runtime replay can still encounter `log.aggregate.md` /
# `status.aggregate.md`. Keeping the suffix exclusion prevents the r6 failure class from returning:
# a prose trace placeholder folded from a descendant log became MALFORMED-TRACE and livelocked a
# finished, reviewer-ACCEPTED product (greenfield-trailmark, root-caused 2026-07-16).
_HARNESS_AGGREGATE_SUFFIX = ".aggregate.md"
_TRACE_STANZA = traceability.TRACE_STANZA
_ID_TOKEN = re.compile(r"\b(?:R-\d+(?:\.\d+)*|DR-\d+\w*)\b")
# Tolerant of markdown emphasis/punctuation between the word and the token ("**Verdict:**
# ACCEPT", "Verdict — accept"): the strict literal-only form made the required token a coin
# flip — 6 MISSING-GATE-VERDICT refuse-retry cycles in the one delivered run (2026-06-17) and
# 3-of-4 verdicts in one r6 subtree, all on reviewers whose verdicts were unambiguous but
# markdown-wrapped. The contract's point is an explicit machine-readable verdict, not exact
# typography (root-caused 2026-07-16).
_GATE_VERDICT = re.compile(r"\bVERDICT\b[\s:*_`—-]{0,8}(ACCEPT|BOUNCE|ESCALATE)\b", re.IGNORECASE)
_CLAIM_ACCOUNT_HEADINGS = (
    "## Drove and Watched",
    "## Inferred",
    "## Residual Uncertainty",
    "## Inventory",
)
_GATE_ARTIFACT_BY_LEVEL = {
    "L5+": "gate-report.md",
    "L4+": "gate-composition-report.md",
    "L4": "gate-composition-report.md",
    "L3+": "gate-area-composition-review.md",
    "L3": "gate-area-composition-review.md",
    "L2+": "gate-composition-review.md",
    "L2": "gate-composition-review.md",
}

EVENT = "return_contract_failed"


@dataclass
class ContractVerdict:
    ok: bool
    defects: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class ContractItem:
    """One stable machine-presence item projected from the deterministic DONE walk."""

    item_id: str
    label: str
    ok: bool
    defects: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractWalk:
    """The single return-contract walk, with verdict and live-checklist projections."""

    items: tuple[ContractItem, ...]
    defects: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.defects

    def verdict(self) -> ContractVerdict:
        return ContractVerdict(ok=self.ok, defects=list(self.defects))


_ITEM_DEFINITIONS = (
    (
        "return_report",
        "Write the required non-empty return report and remove any unfilled-form sentinel.",
        (
            "MISSING-REPORT",
            "MISSING-REVIEW-CHECK-REPORT",
            "UNFILLED-REPORT-FORM",
            "MISSING-REVIEWER-ROUTING",
        ),
    ),
    (
        "gate_artifact",
        "Write the required review gate artifact with an explicit machine-readable verdict.",
        ("MISSING-GATE-ARTIFACT", "MISSING-GATE-VERDICT"),
    ),
    (
        "review_dispatch",
        "Complete the deterministic higher-review plan, packet, check reports, and routing evidence.",
        (
            "MISSING-REVIEW-PACKET",
            "MISSING-REVIEW-PLAN",
            "MISSING-REVIEW-MODE",
            "MISSING-ROLE-SELECTION",
            "MISSING-SHORT-REVIEW-EXCEPTION",
            "INVALID-SHORT-REVIEW-EXCEPTION",
            "MISSING-REVIEWER-REPORT",
            "MISSING-REVIEW-CHECK",
            "REVIEW-CHECK",
            "MISSING-LOWER-EXECUTION",
        ),
    ),
    (
        "requirement_citation",
        "Cite at least one requirement ID supplied in the task package when this seat was given IDs.",
        ("MISSING-REQUIREMENT-CITATION",),
    ),
    (
        "test_author_red_run_log",
        "Record the observed failing run for each new acceptance check in tests/red-run-log.md.",
        ("MISSING-RED-RUN-LOG",),
    ),
    (
        "claim_account",
        "Complete the implementation claim account: drove and watched, inferred, residual "
        "uncertainty, and inventory.",
        ("MISSING-CLAIM-ACCOUNT-SECTIONS",),
    ),
    (
        "trace_structure",
        "Keep every present return-artifact trace stanza structurally valid and non-contradictory.",
        ("MALFORMED-TRACE", "DUP-ID", "TRACE-CONTRADICTION"),
    ),
)


def _defect_class(defect: str) -> str:
    return str(defect).split(":", 1)[0]


def _checklist_items(defects: list[str]) -> tuple[ContractItem, ...]:
    """Project stable checklist items from the exact defect list produced by the ONE walker."""
    by_class: dict[str, list[str]] = {}
    for defect in defects:
        by_class.setdefault(_defect_class(defect), []).append(str(defect))
    items: list[ContractItem] = []
    consumed: set[str] = set()
    for item_id, label, classes in _ITEM_DEFINITIONS:
        item_defects: list[str] = []
        for defect_class in classes:
            item_defects.extend(by_class.get(defect_class, ()))
            consumed.add(defect_class)
        items.append(
            ContractItem(
                item_id=item_id,
                label=label,
                ok=not item_defects,
                defects=tuple(item_defects),
            )
        )
    # A new deterministic defect class must remain checklist-visible even before it receives a
    # friendlier stable item grouping. This keeps the projection complete without duplicating the
    # walker's domain checks.
    for defect_class in sorted(set(by_class) - consumed):
        item_defects = tuple(by_class[defect_class])
        items.append(
            ContractItem(
                item_id=f"contract_{defect_class.lower().replace('_', '-').replace(' ', '-')}",
                label=f"Satisfy the deterministic return-contract requirement {defect_class}.",
                ok=False,
                defects=item_defects,
            )
        )
    return tuple(items)


def _node_dir(node_address: str, *, runtime_root=None) -> Optional[Path]:
    root = runtime_root if runtime_root is not None else ledger.RUNTIME_ROOT
    if root is None:
        return None
    try:
        return addressing.node_dir(node_address, root)
    except (OSError, ValueError):
        return None


def report_stamp(node_address: str) -> Optional[dict]:
    """The LR-23 GATE EVIDENCE-STAMP: sha256 + byte-size of the node's report.md AS IT EXISTS
    RIGHT NOW — ``{"report_sha256": <hex>, "report_bytes": <int>}``, or None when there is
    nothing to stamp (no runtime root, no node dir, no report.md, or an unreadable file).

    Lives HERE deliberately: it hashes the SAME ``<node-dir>/report.md`` this walker reads
    (``check_done_contract`` item 1), so the stamped artifact and the gated artifact cannot be
    two different paths. Two callers (LR-23 remedy a, user-ruled 2026-06-12 — evidence only):

      * ``chokepoint.collapse`` at the accepted-DONE collapse — freeze the FACT of what the gate
        approved into the collapse WAL row's binding_delta + onto the binding;
      * ``promote.promote`` at the accept path — compare the CURRENT hash against the stamp and
        journal ``report_drift`` on mismatch (non-blocking; the operator decides).

    Best-effort by contract: a missing/unreadable report is None, NEVER an exception — the
    substrate paths drive collapse without any tree on disk, and a stamp hiccup must never fail
    a clean collapse or a delivery. Hashes RAW BYTES (no decode), so the stamp is byte-exact.
    """
    node_dir = _node_dir(node_address)
    if node_dir is None:
        return None
    try:
        raw = (node_dir / "report.md").read_bytes()
    except OSError:
        return None
    return {"report_sha256": hashlib.sha256(raw).hexdigest(), "report_bytes": len(raw)}


def artifact_stamp(path: Optional[Path], *, prefix: str = "artifact") -> Optional[dict]:
    """Return a byte-exact stamp for a named artifact path.

    Review seats share the producer's node root, so ``report_stamp(review_address)`` would hash the
    producer's ``report.md``. Gate-owned review artifacts are stamped explicitly by path instead.
    """
    if path is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return {f"{prefix}_sha256": hashlib.sha256(raw).hexdigest(), f"{prefix}_bytes": len(raw)}


def gate_artifact_name(binding: Optional[dict]) -> Optional[str]:
    """Return the review-seat gate artifact for this binding's level, if one is required."""
    if not binding or not binding.get("gate_for"):
        return None
    return _GATE_ARTIFACT_BY_LEVEL.get((binding.get("level") or "").strip())


def gate_artifact_path(
    node_address: str,
    binding: Optional[dict] = None,
    *,
    runtime_root=None,
    bindings: Optional[dict] = None,
) -> Optional[Path]:
    """Return the review-owned gate artifact path under ``reviews/<gate-id>/``.

    Producer and review seats share the same node root. The final review verdict artifact must
    therefore live under the harness-created review directory, with a basename distinct from the
    producer's node-root evidence files.
    """
    node_dir = _node_dir(node_address, runtime_root=runtime_root)
    if node_dir is None:
        return None
    binding = binding or (bindings or {}).get(node_address)
    if binding is None:
        binding = ledger.read_binding(node_address) or {}
    artifact_name = gate_artifact_name(binding)
    if not artifact_name:
        return None
    gate_dir = _resolve_review_gate_dir(node_dir, binding, bindings=bindings)
    if gate_dir is None:
        return None
    return gate_dir / artifact_name


def _expected_gate_artifact_display(
    node_address: str,
    binding: dict,
    artifact_name: str,
    *,
    runtime_root=None,
    bindings: Optional[dict] = None,
) -> str:
    node_dir = _node_dir(node_address, runtime_root=runtime_root)
    if node_dir is None:
        return f"reviews/<gate-id>/{artifact_name}"
    gate_dir = _resolve_review_gate_dir(node_dir, binding, bindings=bindings)
    if gate_dir is not None:
        return str(gate_dir / artifact_name)
    return str(node_dir / "reviews" / "<gate-id>" / artifact_name)


def _resolve_review_gate_dir(
    node_dir: Path,
    binding: dict,
    *,
    bindings: Optional[dict] = None,
) -> Optional[Path]:
    producer_address = (binding or {}).get("gate_for")
    producer = (bindings or {}).get(producer_address) if producer_address else None
    if producer is None and producer_address:
        producer = ledger.read_binding(producer_address)
    if producer:
        if producer.get("gate_review_dir"):
            return Path(producer["gate_review_dir"])
        if producer.get("gate_id"):
            return node_dir / "reviews" / str(producer["gate_id"])
    root = node_dir / "reviews"
    try:
        candidates = sorted(
            p for p in root.iterdir()
            if p.is_dir() and ((p / "review-packet.md").exists() or (p / "review-plan.md").exists())
        )
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def gate_report_verdict(node_address: str, binding: Optional[dict] = None) -> Optional[str]:
    """Return the review gate artifact's explicit verdict, if present.

    The terminal signal carries the verdict for routing, but the level-specific gate artifact is
    authoritative. This parser is deliberately small and deterministic:
    ``VERDICT: ACCEPT``, ``VERDICT: BOUNCE``, or ``VERDICT: ESCALATE``.
    """
    node_dir = _node_dir(node_address)
    if node_dir is None:
        return None
    binding = binding or ledger.read_binding(node_address) or {}
    artifact_path = gate_artifact_path(node_address, binding)
    if artifact_path is None:
        return None
    try:
        text = artifact_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _extract_gate_verdict(text)


def _extract_gate_verdict(text: str) -> Optional[str]:
    match = _GATE_VERDICT.search(text or "")
    return match.group(1).upper() if match else None


def walk_done_contract(
    node_address: str,
    binding: dict,
    *,
    runtime_root=None,
    bindings: Optional[dict] = None,
) -> ContractWalk:
    """Run the ONE deterministic DONE-contract walk and expose both stable projections."""
    node_dir = _node_dir(node_address, runtime_root=runtime_root)
    if node_dir is None or not node_dir.is_dir():
        # No workspace on disk to walk — nothing checkable (e.g. substrate-only test paths).
        return ContractWalk(items=_checklist_items([]), defects=())

    defects: list = []

    if binding.get("semantic_cell_role"):
        role = str(binding.get("semantic_cell_role"))
        report_path = Path(str(binding.get("semantic_report") or ""))
        control_path = Path(str(binding.get("semantic_cell_control") or ""))
        role_defects = plan_alignment_cell.validate_report_path(
            role,
            report_path,
            control_path=control_path,
        )
        defects.extend(
            f"SEMANTIC-SEAT-OUTPUT:{role}:{defect}"
            for defect in role_defects
        )
        return ContractWalk(items=_checklist_items(defects), defects=tuple(defects))

    if review_dispatch.is_review_check_binding(binding):
        report_path_raw = str(binding.get("review_check_report") or "").strip()
        report_path = Path(report_path_raw) if report_path_raw else node_dir / "report.md"
        try:
            report_text = report_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            report_text = ""
        if not report_text:
            defects.append(
                f"MISSING-REVIEW-CHECK-REPORT: {node_address} signed DONE without the assigned "
                f"review-check report at {report_path}"
            )
        elif any(
            "form:unfilled" in line and line.lstrip().startswith("<!--")
            for line in report_text.splitlines()
        ):
            defects.append(
                f"UNFILLED-REPORT-FORM: {node_address} signed DONE with the review-check report "
                "skeleton untouched — fill the report and delete the sentinel before re-signaling"
            )
        elif not review_dispatch.report_has_routing_field(report_text):
            defects.append(
                f"MISSING-REVIEWER-ROUTING: {report_path} must include a plain line "
                "`Recommended Routing: <accept-note|bounce|escalate>`"
            )
        return ContractWalk(items=_checklist_items(defects), defects=tuple(defects))

    level = (binding.get("level") or "").strip()
    artifact_name = gate_artifact_name(binding)
    artifact_text = ""
    artifact_path = (
        gate_artifact_path(
            node_address,
            binding,
            runtime_root=runtime_root,
            bindings=bindings,
        )
        if artifact_name
        else None
    )

    # --- 1. MISSING-REPORT: report.md present + non-empty -----------------------------------
    # Higher composition gates use their named portfolio artifact as the review deliverable; forcing
    # an additional review report.md in the shared #exec/#review node directory pressures the review
    # seat to overwrite the producer's report.md.
    report = node_dir / "report.md"
    report_text = ""
    if not report.is_file() or not (report_text := report.read_text(encoding="utf-8", errors="replace")).strip():
        if not artifact_name:
            defects.append(
                f"MISSING-REPORT: {node_address} signed DONE without a non-empty report.md — the "
                f"parent-facing deliverable every role doc requires (you cannot report complete)"
            )

    # --- 1b. UNFILLED-REPORT-FORM (#30): the pre-instantiated skeleton must never auto-satisfy
    #     this contract — a report still carrying the marker COMMENT line is NOT a report. A
    #     filled report may mention the marker name as evidence that it was removed.
    if report_text and any(
        "form:unfilled" in line and line.lstrip().startswith("<!--")
        for line in report_text.splitlines()
    ):
        defects.append(
            f"UNFILLED-REPORT-FORM: {node_address} signed DONE with the harness-instantiated "
            f"report skeleton untouched (the form:unfilled sentinel is still present) — fill the "
            f"form (replace the prompts, account for each pre-listed ID) and delete the sentinel "
            f"line before re-signaling"
        )

    # --- 1c. BUILD-TO-CLAIM PRESENCE FLOORS -----------------------------------------------
    # Presence only. L5+ owns the judgment: whether each new check was really observed red,
    # whether the scenario drives the claim, and whether the implementation account is honest.
    child_purpose = str(binding.get("child_purpose") or "").strip()
    if child_purpose == "test_author":
        red_run_log = node_dir / "tests" / "red-run-log.md"
        try:
            red_run_text = red_run_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            red_run_text = ""
        if not red_run_text.strip():
            defects.append(
                f"MISSING-RED-RUN-LOG: {node_address} signed DONE as a test-author without a "
                f"non-empty {red_run_log} — record the observed failing run for each new check; "
                "L5+ judges the evidence"
            )
    elif level == "L5":
        report_lines = set(report_text.splitlines())
        missing_headings = [
            heading for heading in _CLAIM_ACCOUNT_HEADINGS if heading not in report_lines
        ]
        if missing_headings:
            defects.append(
                f"MISSING-CLAIM-ACCOUNT-SECTIONS: {node_address} signed DONE as an implementation "
                "L5 without exact report headings "
                + ", ".join(f"`{heading}`" for heading in missing_headings)
                + " — presence is the runtime floor; L5+ judges the account"
            )

    # --- 1d. REVIEW GATE ARTIFACT: the level-specific artifact owns the routing verdict -------
    if artifact_name:
        if artifact_path is not None and artifact_path.is_file():
            artifact_text = artifact_path.read_text(encoding="utf-8", errors="replace")
        if not artifact_text.strip():
            display = _expected_gate_artifact_display(
                node_address,
                binding,
                artifact_name,
                runtime_root=runtime_root,
                bindings=bindings,
            )
            defects.append(
                f"MISSING-GATE-ARTIFACT: {node_address} signed DONE as a {level} review gate "
                f"without a non-empty {display} — this level's review-owned gate artifact "
                "owns the ACCEPT/BOUNCE/ESCALATE judgment"
            )
        if artifact_text.strip() and _extract_gate_verdict(artifact_text) is None:
            defects.append(
                f"MISSING-GATE-VERDICT: {node_address} signed DONE as a {level} review gate "
                f"without an explicit `VERDICT: ACCEPT`, `VERDICT: BOUNCE`, or "
                f"`VERDICT: ESCALATE` in {artifact_path or artifact_name} — the terminal signal "
                "may carry the verdict, but the gate artifact is authoritative"
            )

    # --- 1e. HIGHER REVIEW DISPATCH ARTIFACTS: plan + check reports or short exception ------
    defects.extend(review_dispatch.dispatch_contract_defects(node_address, binding))

    # --- 2. MISSING-REQUIREMENT-CITATION (L5-class, only when IDs were GIVEN) ----------------
    citation_text = artifact_text if artifact_name else report_text
    if level in ("L5", "L5+") and citation_text:
        given: set = set()
        for name in ("brief.md", "acceptance.md"):
            f = node_dir / name
            if f.is_file():
                given.update(_ID_TOKEN.findall(f.read_text(encoding="utf-8", errors="replace")))
        if given and not (given & set(_ID_TOKEN.findall(citation_text))):
            defects.append(
                f"MISSING-REQUIREMENT-CITATION: {node_address} gate artifact cites NONE of the "
                f"requirement IDs given in its brief/acceptance ({sorted(given)[:8]}) — the L5+ "
                f"reviewer cannot confirm spec-fidelity against an unstated target (L5 role §Outputs)"
            )

    # --- 3. Trace-block stanzas parse where present ------------------------------------------
    defects.extend(_check_trace_stanzas(node_address, node_dir))

    return ContractWalk(items=_checklist_items(defects), defects=tuple(defects))


def check_done_contract(node_address: str, binding: dict) -> ContractVerdict:
    """The unchanged public DONE verdict, projected from :func:`walk_done_contract`."""
    return walk_done_contract(node_address, binding).verdict()


def _parse_stanza(raw: str) -> tuple[Optional[dict], Optional[str]]:
    """Parse one relaxed trace stanza body ('id: R-1.2, serves: [R-1], kind: requirement, …').
    Returns (fields, error)."""
    return traceability.parse_stanza(raw)


def _check_trace_stanzas(node_address: str, node_dir: Path) -> list:
    defects: list = []
    seen_ids: dict = {}
    try:
        md_files = sorted(
            p for p in node_dir.iterdir()
            if (
                p.suffix == ".md"
                and p.is_file()
                and not p.name.startswith(".")
                and p.name not in _TRACE_NON_RETURN_FILES
                and not p.name.endswith(_HARNESS_AGGREGATE_SUFFIX)
            )
        )
    except OSError:
        return defects
    for md in md_files:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _TRACE_STANZA.finditer(text):
            fields, error = _parse_stanza(match.group(1))
            if error:
                defects.append(f"MALFORMED-TRACE: {md.name} in {node_address}: {error}")
                continue
            tid = fields["id"]
            if tid in seen_ids:
                defects.append(
                    f"DUP-ID: {tid} appears in both {seen_ids[tid]} and {md.name} ({node_address})"
                )
            seen_ids[tid] = md.name
            serves = fields.get("serves") or []
            if fields.get("kind") == "requirement" and "." in tid and serves:
                if not any(tid == s or tid.startswith(s + ".") for s in serves):
                    defects.append(
                        f"TRACE-CONTRADICTION: {tid} ({md.name}, {node_address}) does not truncate "
                        f"into any declared serves id {serves} — the dotted prefix IS the upward "
                        f"trace link (PLAN-ALIGNMENT-GATE)"
                    )
    return defects


# ---------------------------------------------------------------------------
# Edge-triggered defect journaling + the inbox nudge line.
# ---------------------------------------------------------------------------

def refusal_for_signal_artifact(
    node_address: str, signal_artifact_seen_at: Optional[str]
) -> Optional[list]:
    """Return prior defects for this refused signal artifact identity, if any.

    The return-contract edge is keyed by the signal artifact the harness observed, not by the
    mutable files the signal points at. If a node edits report/trace files before rewriting the
    signal, the old refused signal must stay refused; otherwise the watchdog can route a candidate
    under stale evidence while the producer is still in the repair turn.
    """
    if not signal_artifact_seen_at:
        return None
    try:
        for row in ledger.load_wal():
            if row.get("event") != EVENT or row.get("node_address") != node_address:
                continue
            delta = row.get("binding_delta") or {}
            if delta.get("signal_artifact_seen_at") == signal_artifact_seen_at:
                return list(delta.get("defects") or [])
    except Exception:  # noqa: BLE001 — unreadable history cannot prove a sticky refusal
        return None
    return None


def _already_journaled(node_address: str, signal_artifact_seen_at: str) -> bool:
    return refusal_for_signal_artifact(node_address, signal_artifact_seen_at) is not None


def _defects_include_candidate_artifact_drift(defects: list) -> bool:
    return any(
        str(defect).startswith(("CANDIDATE-ARTIFACT-ADDED:", "CANDIDATE-ARTIFACT-DRIFT:"))
        for defect in defects
    )


def _defects_include_stale_contract_receipt(defects: list) -> bool:
    return any(str(defect).startswith("STALE-CONTRACT-RECEIPT:") for defect in defects)


def _defect_message(defects: list) -> str:
    defect_text = " | ".join(str(d) for d in list(defects)[:5])
    if _defects_include_stale_contract_receipt(defects):
        return (
            "Your candidate submission was REFUSED because its contract receipt is stale. "
            "Defects: "
            + defect_text
            + " — recovery: re-read the named revision record, write your contract-rebind "
            "marker, then resubmit the candidate with a fresh .signal timestamp."
        )
    if _defects_include_candidate_artifact_drift(defects):
        return (
            "Your DONE sign-off was REFUSED by the return contract. Defects: "
            + defect_text
            + " — the candidate identity is invalid. Stop this verdict attempt; the parent or "
            "producer must submit a fresh candidate before a review verdict can route. Wait for a "
            "new candidate_submitted pointer or an explicit retry before reviewing again."
        )
    return (
        "Your DONE sign-off was REFUSED by the return contract. Defects: "
        + defect_text
        + " — fix the named artifact(s), then re-write your .signal file (same "
        "owner_token, fresh ts)."
    )


# ---------------------------------------------------------------------------------------
# THE GATE-LOOP CIRCUIT BREAKER (2026-07-16, from the r6 root-cause). r6 spun forever on
# identical return-contract refusals — no bounce ceiling existed anywhere, `gate_bounce_count`
# never incremented on contract refusals, and nothing escalated to a human; a finished,
# reviewer-ACCEPTED product livelocked until the daemon was killed. The breaker converts an
# unbounded identical-refusal loop into a bounded, LOUDLY surfaced stall: after
# CIRCUIT_BREAKER_THRESHOLD refusals with the SAME defect signature on one node, the node is
# PAUSED (the §5.3 primitive every spawner/watchdog path already honors) and a
# `gate_loop_circuit_broken` row names the loop for the operator. Resume is a human act
# (harnessctl resume) after fixing the cause — never automatic.
# ---------------------------------------------------------------------------------------

CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_EVENT = "gate_loop_circuit_broken"


def _defect_signature(defects: list) -> tuple:
    """The failure-class signature of a refusal: the sorted set of defect type prefixes.

    Two refusals are 'identical' when the same defect CLASSES fire — the detail text varies
    per attempt (paths, hashes, timestamps), the class set is what loops."""
    return tuple(sorted({str(d).split(":", 1)[0] for d in defects}))


def _identical_refusal_count(node_address: str, signature: tuple) -> int:
    """Count journaled return-contract refusals on this node with the same defect signature."""
    count = 0
    try:
        for row in ledger.load_wal():
            if row.get("event") != EVENT or row.get("node_address") != node_address:
                continue
            delta = row.get("binding_delta") or {}
            if _defect_signature(list(delta.get("defects") or [])) == signature:
                count += 1
    except Exception:  # noqa: BLE001 — an unreadable WAL cannot arm the breaker
        return 0
    return count


def _l1_root_address() -> Optional[str]:
    """The run's L1 root exec address (the human's proxy — owner ruling 2026-07-17: exceptional
    interventions route THROUGH L1, never straight past it)."""
    try:
        for address, binding in ledger.all_nodes().items():
            if (binding or {}).get("level") == "L1" and address.endswith("#exec"):
                return address
    except Exception:  # noqa: BLE001
        return None
    return None


def _journal_and_notify_l1(
    node_address: str,
    binding: dict,
    *,
    event: str,
    binding_delta: dict,
    summary: str,
    inbox_row: dict,
) -> None:
    """Reuse the durable WAL + L1-inbox intervention route for one typed condition."""
    try:
        executor.journal(
            node_address,
            event=event,
            from_state=binding.get("state"),
            to_state=binding.get("state"),
            lease_epoch=binding.get("lease_epoch"),
            binding_delta=binding_delta,
            summary=summary,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        l1 = _l1_root_address()
        if l1 and ledger.RUNTIME_ROOT is not None and l1 != node_address:
            inbox = addressing.inbox_path(l1, ledger.RUNTIME_ROOT)
            inbox.parent.mkdir(parents=True, exist_ok=True)
            line = {"from": "harnessd", **inbox_row, "ts": clock.now_utc()}
            with inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _break_gate_loop(node_address: str, binding: dict, signature: tuple, count: int) -> None:
    """Pause the looping node, journal the row, and ESCALATE TO L1 (owner ruling 2026-07-17:
    interventions come through L1 — the daemon informs the human's proxy, it does not page the
    human directly). Best-effort by design: a breaker hiccup must never convert a refusal into
    a collapse (same rule as the defect journaling)."""
    try:
        executor.pause(node_address, paused_at=clock.now_utc())
    except Exception:  # noqa: BLE001
        pass
    _journal_and_notify_l1(
        node_address,
        binding,
        event=CIRCUIT_BREAKER_EVENT,
        binding_delta={
            "circuit_broken_signature": list(signature),
            "circuit_broken_count": count,
        },
        summary=(
            f"GATE-LOOP CIRCUIT BROKEN on {node_address}: {count} return-contract refusals "
            f"with identical defect signature {'/'.join(signature) or 'unknown'} — node PAUSED; "
            "escalated to L1 (r6-class livelock containment, 2026-07-16; L1 routing 2026-07-17)."
        ),
        inbox_row={
            "type": "gate_loop_circuit_broken",
            "message": (
                f"GATE-LOOP CIRCUIT BROKEN: {node_address} was refused by the return "
                f"contract {count} times with the identical defect signature "
                f"{'/'.join(signature) or 'unknown'} and has been PAUSED to stop the loop. "
                "You own the disposition: inspect the named defects in the run ledger, "
                "direct a repair or resume via harnessctl, or escalate to the client if "
                "this cannot be resolved at your altitude."
            ),
        },
    )


def elevate_gate_nonconvergence(
    node_address: str,
    binding: dict,
    *,
    gate_id: str,
    review_address: str,
    count: int,
) -> None:
    """Raise the fifth parent-judgment failure through the existing L1 intervention route."""
    message = (
        f"Gate {node_address} ({gate_id or 'unknown gate id'}) reached escalation count "
        f"{count} after review {review_address}: parent judgment is not converging. "
        "L1 must decide whether to re-plan, kill, or rule."
    )
    _journal_and_notify_l1(
        node_address,
        binding,
        event="gate_escalation_nonconverging",
        binding_delta={
            "gate_id": gate_id,
            "gate_review_address": review_address,
            "gate_escalation_count": count,
            "threshold": 5,
        },
        summary=message,
        inbox_row={
            "type": "gate_escalation_nonconverging",
            "gate": node_address,
            "gate_id": gate_id,
            "review": review_address,
            "gate_escalation_count": count,
            "message": message,
        },
    )


def journal_defects_once(
    node_address: str,
    binding: dict,
    signal_artifact_seen_at: Optional[str],
    defects: list,
    *,
    agent_signal_ts: Optional[str] = None,
) -> bool:
    """Land the typed-defect row + the inbox defect line once per signal artifact identity.

    Returns True when this call journaled (first detection), False on the steady-state re-poll.
    Both writes are best-effort: a journaling hiccup never converts a refusal into a collapse.
    """
    signal_artifact_seen_at = signal_artifact_seen_at or ""
    agent_signal_ts = agent_signal_ts or ""
    if _already_journaled(node_address, signal_artifact_seen_at):
        return False
    try:
        executor.journal(
            node_address,
            event=EVENT,
            from_state=binding.get("state"),
            to_state=binding.get("state"),
            lease_epoch=binding.get("lease_epoch"),
            binding_delta={
                "defects": list(defects)[:10],
                "signal_artifact_seen_at": signal_artifact_seen_at,
                "agent_signal_ts": agent_signal_ts,
            },
            summary=(
                f"return contract REFUSED collapse for {node_address} "
                f"(signal artifact {signal_artifact_seen_at or 'unknown'}, agent ts {agent_signal_ts or 'unknown'}): "
                + "; ".join(str(d)[:160] for d in list(defects)[:5])
            ),
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        if ledger.RUNTIME_ROOT is not None:
            inbox = addressing.inbox_path(node_address, ledger.RUNTIME_ROOT)
            inbox.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({
                "from": "harnessd",
                "type": "return_contract_defect",
                "message": _defect_message(defects),
                "ts": clock.now_utc(),
            })
            with inbox.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    # THE CIRCUIT BREAKER: fire exactly once, at the threshold-th identical-signature refusal
    # (the count includes the row journaled above). A node already paused never re-trips.
    signature = _defect_signature(list(defects))
    if not binding.get("paused_at"):
        count = _identical_refusal_count(node_address, signature)
        if count == CIRCUIT_BREAKER_THRESHOLD:
            _break_gate_loop(node_address, binding, signature, count)
    return True
