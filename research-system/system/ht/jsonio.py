"""Canonical JSON serialization for state files.

Insertion order is preserved (readability); 2-space indent; UTF-8; trailing
newline. The authority diff compares by key, so field order is not load-bearing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def dumps(doc: Any) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def dump(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(doc), encoding="utf-8")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_or_none(path: Path) -> Any | None:
    if not path.exists():
        return None
    return load(path)
