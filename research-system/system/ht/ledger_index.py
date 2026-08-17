"""Deterministic generated union view over every federated ledger book.

``ledger/union.index.json`` is a mechanical projection, never a citable state
source.  Commands regenerate it after every ledger-entry write; validation uses
the same pure builder so drift cannot acquire a second definition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import jsonio
from .paths import Root


LEDGER_SECTIONS = ("user", "research", "observatory")


def invariant_errors(root: Root) -> list[str]:
    """Validate the union-global identity invariant against canonical entries."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    if not root.ledger_dir.is_dir():
        return errors
    for book_dir in sorted(p for p in root.ledger_dir.iterdir() if p.is_dir()):
        for section in LEDGER_SECTIONS:
            section_dir = book_dir / section
            if not section_dir.is_dir():
                continue
            for path in sorted(section_dir.glob("L-*.json")):
                entry = jsonio.load(path)
                rel = path.relative_to(root.path).as_posix()
                entry_id = entry.get("id")
                if entry_id != path.stem:
                    errors.append(
                        f"{rel}: ledger id {entry_id!r} does not match filename "
                        f"{path.stem!r} (macro §5 global namespace)"
                    )
                if isinstance(entry_id, str) and entry_id in seen:
                    errors.append(
                        f"{rel}: duplicate union-global ledger id {entry_id!r}; "
                        f"already present at {seen[entry_id]} (macro §5)"
                    )
                elif isinstance(entry_id, str):
                    seen[entry_id] = rel
    return errors


def build_from_documents(
    documents: list[tuple[str, str, str, dict[str, Any]]],
) -> dict[str, Any]:
    """Build the canonical union index from one caller-frozen document view."""
    entries: list[dict[str, Any]] = []
    for book, section, relative, entry in documents:
        entries.append(
            {
                "id": entry["id"],
                "book": book,
                "section": section,
                "path": relative,
                "text": entry["text"],
                "status": entry["status"],
            }
        )

    # Path is globally unique and gives stable ordering even while validation is
    # diagnosing an illicit duplicate global ID.
    entries.sort(
        key=lambda item: (
            item["id"],
            item["book"],
            item["section"],
            item["path"],
        )
    )
    return {"generated": True, "citable": False, "entries": entries}


def build(root: Root) -> dict[str, Any]:
    """Build the canonical union index from ledger entries without writing it."""
    documents: list[tuple[str, str, str, dict[str, Any]]] = []
    if root.ledger_dir.is_dir():
        for book_dir in sorted(p for p in root.ledger_dir.iterdir() if p.is_dir()):
            for section in LEDGER_SECTIONS:
                section_dir = book_dir / section
                if not section_dir.is_dir():
                    continue
                for path in sorted(section_dir.glob("L-*.json")):
                    documents.append(
                        (
                            book_dir.name,
                            section,
                            path.relative_to(root.path).as_posix(),
                            jsonio.load(path),
                        )
                    )
    return build_from_documents(documents)


def regenerate(root: Root) -> Path:
    """Regenerate the union index wholesale and return its path."""
    jsonio.dump(root.ledger_union_index, build(root))
    return root.ledger_union_index
