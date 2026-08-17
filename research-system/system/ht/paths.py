"""Research-root resolution and the A2 §2 physical layout.

A research root is addressed by --root, then $HT_ROOT, then by walking up from
cwd looking for a marker (system/schemas/ + trees/). Tests point --root/$HT_ROOT
at tmp sandboxes built by `ht root init`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import HtUsageError


def normalize_repository_relpath(value: str) -> str:
    """Return one exact safe POSIX repository-relative spelling.

    B2 repository paths are identities, not merely joinable strings.  Reject
    aliases, platform separators, and every ASCII control byte before any path
    object or filesystem operation can normalize them invisibly.
    """

    if not isinstance(value, str) or not value:
        raise HtUsageError("repository-relative path must be a non-empty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise HtUsageError(f"repository-relative path contains a control byte: {value!r}")
    if value.startswith("/") or "\\" in value or "//" in value:
        raise HtUsageError(f"repository-relative path is not normalized: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HtUsageError(f"repository-relative path is not normalized: {value!r}")
    normalized = Path(*parts).as_posix()
    if normalized != value:
        raise HtUsageError(f"repository-relative path aliases {normalized!r}: {value!r}")
    return value


def find_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("HT_ROOT")
    if env:
        return Path(env).resolve()
    cur = Path.cwd().resolve()
    for cand in [cur, *cur.parents]:
        if (cand / "system" / "schemas").is_dir() and (cand / "trees").is_dir():
            return cand
    raise HtUsageError(
        "no research root found (looked for system/schemas + trees walking up "
        "from cwd); pass --root or set HT_ROOT"
    )


@dataclass(frozen=True)
class Root:
    """Path helpers for a single research root (A2 §2 layout)."""

    path: Path

    # --- top-level lanes ---
    @property
    def schemas_dir(self) -> Path:
        return self.path / "system" / "schemas"

    @property
    def registry(self) -> Path:
        return self.path / "system" / "instruments" / "registry.json"

    @property
    def trees_dir(self) -> Path:
        return self.path / "trees"

    @property
    def ledger_dir(self) -> Path:
        return self.path / "ledger"

    @property
    def readout_dir(self) -> Path:
        return self.path / "readout"

    @property
    def tier1_dir(self) -> Path:
        return self.path / "tier1"

    @property
    def worktrees_dir(self) -> Path:
        return self.path / "worktrees"

    @property
    def global_lock_path(self) -> Path:
        """Root-scoped mutex shared by merge and ledger-write operations."""
        return self.path / ".ht-global.lock"

    @property
    def hook_path(self) -> Path:
        return self.path / ".git" / "hooks" / "pre-commit"

    # --- tree-scoped ---
    def tree_dir(self, component: str) -> Path:
        return self.trees_dir / component

    def tree_json(self, component: str) -> Path:
        return self.tree_dir(component) / "tree.json"

    def index_json(self, component: str) -> Path:
        return self.tree_dir(component) / "index.json"

    def index_live_json(self, component: str) -> Path:
        return self.tree_dir(component) / "index.live.json"

    def nodes_dir(self, component: str) -> Path:
        return self.tree_dir(component) / "nodes"

    # --- node-scoped ---
    def node_dir(self, component: str, node_id: str) -> Path:
        return self.nodes_dir(component) / node_id

    def node_json(self, component: str, node_id: str) -> Path:
        return self.node_dir(component, node_id) / "node.json"

    def dispatch_json(self, component: str, node_id: str, dispatch_id: str) -> Path:
        return self.node_dir(component, node_id) / "dispatches" / f"{dispatch_id}.json"

    def reports_dir(self, component: str, node_id: str) -> Path:
        return self.node_dir(component, node_id) / "reports"

    def adjudication_path(
        self, component: str, node_id: str, adjudication_id: str
    ) -> Path:
        return (
            self.node_dir(component, node_id)
            / "adjudications"
            / f"{adjudication_id}.md"
        )

    def archive_dir(self, component: str, node_id: str) -> Path:
        return self.node_dir(component, node_id) / "archive"

    # --- tier-1 coordinator state ---
    @property
    def phase_json(self) -> Path:
        return self.tier1_dir / "phase.json"

    def issue_json(self, issue_id: str) -> Path:
        return self.tier1_dir / "issues" / f"{issue_id}.json"

    @property
    def subgoals_dir(self) -> Path:
        return self.tier1_dir / "subgoals"

    def subgoal_json(self, subgoal_id: str) -> Path:
        return self.subgoals_dir / f"{subgoal_id}.json"

    @property
    def task_packages_dir(self) -> Path:
        return self.tier1_dir / "task-packages"

    def task_package_json(self, package_id: str) -> Path:
        return self.task_packages_dir / f"{package_id}.json"

    @property
    def inbox_deliveries_dir(self) -> Path:
        return self.tier1_dir / "inbox" / "deliveries"

    def inbox_delivery_json(self, delivery_id: str) -> Path:
        return self.inbox_deliveries_dir / f"{delivery_id}.json"

    @property
    def inbox_receipts_dir(self) -> Path:
        return self.tier1_dir / "inbox" / "receipts"

    def inbox_receipt_json(self, delivery_id: str) -> Path:
        return self.inbox_receipts_dir / f"{delivery_id}.json"

    @property
    def action_receipts_dir(self) -> Path:
        return self.tier1_dir / "action-receipts"

    def action_receipt_json(self, receipt_id: str) -> Path:
        return self.action_receipts_dir / f"{receipt_id}.json"

    def pc_decision_json(self, decision_id: str) -> Path:
        return self.tier1_dir / "decision-log" / f"{decision_id}.json"

    @property
    def issue_queue_json(self) -> Path:
        return self.tier1_dir / "issue-queue.json"

    def interrupt_json(self, interrupt_id: str) -> Path:
        return self.tier1_dir / "interrupts" / f"{interrupt_id}.json"

    def ratification_item_json(self, item_id: str) -> Path:
        return self.tier1_dir / "ratification-queue" / f"{item_id}.json"

    def merge_record_json(self, record_id: str) -> Path:
        return self.tier1_dir / "merge-records" / f"{record_id}.json"

    def gate_review_json(self, review_id: str) -> Path:
        return self.tier1_dir / "gate-reviews" / f"{review_id}.json"

    def ledger_book_dir(self, book: str) -> Path:
        return self.ledger_dir / book

    def ledger_entry(self, book: str, section: str, entry_id: str) -> Path:
        return self.ledger_book_dir(book) / section / f"{entry_id}.json"

    @property
    def ledger_union_index(self) -> Path:
        return self.ledger_dir / "union.index.json"

    @property
    def composed_tree_json(self) -> Path:
        return self.readout_dir / "composed-tree.json"

    def observatory_report_card(self, run_id: str) -> Path:
        return self.readout_dir / "observatory" / run_id / "report-card.md"

    def rel(self, p: Path) -> str:
        """POSIX research-root-relative path (the anchor + hook path form)."""
        return p.resolve().relative_to(self.path).as_posix()

    def resolve_rel(self, rel_path: str) -> Path:
        # Legacy callers deliberately resolve first and apply their own
        # normalized-path policy and diagnostics (notably anchor R-i6-3).
        # B2 identity-bearing paths call normalize_repository_relpath directly
        # before this generic filesystem helper.
        return (self.path / rel_path).resolve()
