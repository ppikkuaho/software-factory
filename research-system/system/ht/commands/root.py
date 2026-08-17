"""`ht root init` (harness) — scaffold a reproducible research root (A2 §2).

Creates the directory layout, copies system/schemas, installs the empty instruments
registry, writes .gitignore, installs the pre-commit backstop, runs `git init` if
needed, and makes an initial harness-stamped commit. Writes NO tree state.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .. import composed_tree, gitutil, jsonio, ledger_index
from ..errors import HtError
from ..paths import Root

# Package-relative default schema source: system/ht/commands/ -> system/schemas
_DEFAULT_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"

_GITIGNORE = """\
# hypothesis-tree research root
trees/*/nodes/*/archive/
worktrees/
var/
.ht-global.lock
.ht-repository-retired-*
__pycache__/
*.pyc
"""

_EMPTY_REGISTRY = {"suites": []}


def install_hook(root: Root) -> str:
    return gitutil.install_precommit_hook(root.path)


def _ensure_git(root: Root) -> None:
    if not (root.path / ".git").exists():
        gitutil.run(root.path, ["init", "-q"])
    # local identity for reproducible sandboxes (only if unset)
    if gitutil.run(root.path, ["config", "user.email"], check=False).returncode != 0:
        gitutil.run(root.path, ["config", "user.email", "ht@local"])
    if gitutil.run(root.path, ["config", "user.name"], check=False).returncode != 0:
        gitutil.run(root.path, ["config", "user.name", "ht"])


def run(root: Root, schemas_src: str | None) -> None:
    src = Path(schemas_src).resolve() if schemas_src else _DEFAULT_SCHEMAS
    if not src.is_dir():
        raise HtError(f"schema source '{src}' is not a directory (pass --schemas)")

    # directory layout (A2 §2)
    (root.path / "system").mkdir(parents=True, exist_ok=True)
    root.schemas_dir.mkdir(parents=True, exist_ok=True)
    root.registry.parent.mkdir(parents=True, exist_ok=True)
    root.trees_dir.mkdir(parents=True, exist_ok=True)
    for section in ("user", "research", "observatory"):
        (root.ledger_book_dir("top") / section).mkdir(parents=True, exist_ok=True)
    for store in (
        "issues", "decision-log", "ratification-queue", "merge-records",
        "gate-reviews", "interrupts"
    ):
        (root.tier1_dir / store).mkdir(parents=True, exist_ok=True)
    root.readout_dir.mkdir(parents=True, exist_ok=True)
    root.worktrees_dir.mkdir(parents=True, exist_ok=True)

    # copy schemas — skip files already at the destination: running root init
    # inside the research root itself (in-place genesis / re-init) makes
    # src == dst, and copy2 would raise SameFileError mid-scaffold
    for f in src.glob("*.json"):
        dst = root.schemas_dir / f.name
        if f.resolve() == dst.resolve():
            continue
        shutil.copy2(f, dst)

    # empty instruments registry stub (U1) — only if absent
    if not root.registry.exists():
        jsonio.dump(root.registry, _EMPTY_REGISTRY)

    # Seed the generated, uncitable top union view even before the first entry so
    # a fresh root validates cleanly and every later ledger write replaces it.
    ledger_index.regenerate(root)
    jsonio.dump(root.issue_queue_json, {"entries": []})
    composed_tree.regenerate(root)

    # Spawn surfaces are resolvable in a new root even before the first
    # observatory/statistics pass. They are explicit empty-state pointers, not
    # invented findings.
    jsonio.dump(root.readout_dir / "statistics.json", {"generated": True, "metrics": {}})
    (root.readout_dir / "INTERPRETATION.md").write_text(
        "# Interpretation rules\n\nNo statistics have been produced yet. "
        "Readouts are signals for judgment, never verdicts.\n",
        encoding="utf-8",
    )

    # .gitignore + .gitkeep markers for otherwise-empty tracked dirs
    (root.path / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    for keep in (
        root.trees_dir / ".gitkeep",
        root.ledger_book_dir("top") / "user" / ".gitkeep",
        root.ledger_book_dir("top") / "research" / ".gitkeep",
        root.ledger_book_dir("top") / "observatory" / ".gitkeep",
        root.tier1_dir / "issues" / ".gitkeep",
        root.tier1_dir / "decision-log" / ".gitkeep",
        root.tier1_dir / "ratification-queue" / ".gitkeep",
        root.tier1_dir / "merge-records" / ".gitkeep",
        root.tier1_dir / "gate-reviews" / ".gitkeep",
        root.tier1_dir / "interrupts" / ".gitkeep",
        root.readout_dir / ".gitkeep",
    ):
        keep.write_text("", encoding="utf-8")

    _ensure_git(root)
    install_hook(root)

    # initial harness commit so HEAD exists and the scaffold is auditable
    gitutil.run(root.path, ["add", "-A"])
    if gitutil.run(root.path, ["diff", "--cached", "--quiet"], check=False).returncode != 0:
        full_msg = "ht root init: scaffold research root (A2 §2)\n\nHT-Role: harness\n"
        r = gitutil.run(
            root.path,
            ["commit", "-m", full_msg],
            env_extra={
                "HT_COMMIT": "1",
                "HT_ROLE": "harness",
                "HT_PYTHON": sys.executable,
            },
            check=False,
        )
        if r.returncode != 0:
            raise HtError(f"ht root init commit failed:\n{r.stdout}\n{r.stderr}".strip())
