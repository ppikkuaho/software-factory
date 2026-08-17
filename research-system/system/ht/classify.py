"""Research-root-relative path classification (shared by validate + the hook)."""

from __future__ import annotations

import re

# doc_type patterns on POSIX research-root-relative paths
_PATTERNS = [
    ("tree", re.compile(r"^trees/[^/]+/tree\.json$")),
    ("index", re.compile(r"^trees/[^/]+/index(\.live)?\.json$")),
    ("node", re.compile(r"^trees/[^/]+/nodes/[^/]+/node\.json$")),
    ("dispatch", re.compile(r"^trees/[^/]+/nodes/[^/]+/dispatches/[^/]+\.json$")),
    (
        "ledger_entry",
        re.compile(r"^ledger/[^/]+/(user|research|observatory)/L-\d+\.json$"),
    ),
    ("ledger_union_index", re.compile(r"^ledger/union\.index\.json$")),
    ("phase", re.compile(r"^tier1/phase\.json$")),
    ("issue", re.compile(r"^tier1/issues/I-\d+\.json$")),
    ("pc_decision", re.compile(r"^tier1/decision-log/PCD-\d+\.json$")),
    ("ratification_item", re.compile(r"^tier1/ratification-queue/RQ-\d+\.json$")),
    ("merge_record", re.compile(r"^tier1/merge-records/MR-\d+\.json$")),
    ("gate_review", re.compile(r"^tier1/gate-reviews/GR-\d+\.json$")),
    ("issue_queue", re.compile(r"^tier1/issue-queue\.json$")),
    ("interrupt", re.compile(r"^tier1/interrupts/INT-\d+\.json$")),
    ("composed_tree", re.compile(r"^readout/composed-tree\.json$")),
]

_REPORT = re.compile(r"^trees/[^/]+/nodes/[^/]+/reports/.+\.md$")
_ARCHIVE = re.compile(r"^trees/[^/]+/nodes/[^/]+/archive/.+$")
_ADJUDICATION = re.compile(r"^trees/[^/]+/nodes/[^/]+/adjudications/.+$")
_OBSERVATORY_REPORT = re.compile(
    r"^readout/observatory/[^/]+/report-card\.md$"
)
_LEDGER_ENTRY = re.compile(
    r"^ledger/(?P<book>[^/]+)/(?P<section>user|research|observatory)/"
)

STATE_LANES = ("trees/", "ledger/", "readout/", "tier1/")
_GENERATED_INDEX_BASENAMES = ("index.json", "index.live.json")


def is_generated_view(rel_path: str) -> bool:
    """[R-i6-3] generated-view path classes — banned as claim-anchor targets.

    The casing-blind check deliberately imposes the theoretical over-ban of a
    literal uppercase or mixed-case READOUT directory on case-sensitive volumes,
    consistent with the mechanical, most-restrictive ban posture.
    """
    rel_path = rel_path.lower()
    if rel_path == "readout" or rel_path.startswith("readout/"):
        return True
    base = rel_path.rsplit("/", 1)[-1]
    return base in _GENERATED_INDEX_BASENAMES or base.endswith(".index.json")


def doc_type(rel_path: str) -> str | None:
    for name, pat in _PATTERNS:
        if pat.match(rel_path):
            return name
    return None


def is_report(rel_path: str) -> bool:
    return bool(_REPORT.match(rel_path))


def is_archive(rel_path: str) -> bool:
    return bool(_ARCHIVE.match(rel_path))


def is_adjudication(rel_path: str) -> bool:
    return bool(_ADJUDICATION.match(rel_path))


def is_observatory_report(rel_path: str) -> bool:
    return bool(_OBSERVATORY_REPORT.fullmatch(rel_path))


def in_state_lane(rel_path: str) -> bool:
    return rel_path.startswith(STATE_LANES)


def ledger_section(rel_path: str) -> str | None:
    m = _LEDGER_ENTRY.match(rel_path)
    return m.group("section") if m else None


def ledger_book(rel_path: str) -> str | None:
    m = _LEDGER_ENTRY.match(rel_path)
    return m.group("book") if m else None
