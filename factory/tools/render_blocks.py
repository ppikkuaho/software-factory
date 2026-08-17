#!/usr/bin/env python3
"""render_blocks — render/check the shared doc-blocks registry into the per-level role docs.

THE MECHANISM (design/DOC-SYSTEM.md; prior art: working-notes/splice_gate_contracts.py): each
cross-level duty lives ONCE under ``operational/shared/blocks/`` — a base content file, plus named
per-level adaptation files (``<block-id>.<level>.md``) where a level carries a deliberate variant.
The registry (``operational/shared/blocks/registry.json``) records, per block: the landed version,
the source, and the carrier docs (verbatim or adapted, with the anchor heading a not-yet-landed
block splices before). Carrier docs hold RENDERED COPIES between HTML-comment markers::

    <!-- block:report-contract v1 -->
    ...rendered copy...
    <!-- /block:report-contract -->

Everything OUTSIDE the marker pairs is the level's own craft — this tool NEVER touches it.

TWO MODES, both idempotent:
  * ``--check`` (default) — walk the registry; print typed defects and exit 1 on any of:
    MISSING-BLOCK (registered carrier lacks the marker), STALE-VERSION (marker vN != registry),
    CONTENT-DRIFT (in-marker text != source), UNKNOWN-MARKER (a block marker the registry doesn't
    know, anywhere under operational/), LEGACY-MARKER (a pre-registry LR-13 marker), DUPLICATE-
    MARKER, UNCLOSED-MARKER, MISSING-SOURCE / MISSING-DOC / MISSING-TEMPLATE / BAD-REGISTRY.
  * ``--render`` — re-render every carrier's in-marker content from its source (healing drift,
    stamping the registry version); splice a not-yet-landed block immediately BEFORE its registered
    anchor. A missing/ambiguous anchor ABORTS loudly (RenderError) — no partial landing, per the
    splice prior-art. Running render twice is a no-op.

RUN ONLY BETWEEN RUNS: role docs are read in place by live agents — never re-render mid-run.

CRITICAL CONSTRAINT (user ruling, 2026-06-12 — restate, never weaken): **a green check certifies
MECHANICAL CONFORMANCE ONLY** — no drift/omission accidents in the registered landings. It does
NOT certify that the documentation is healthy, well-aimed, or behaviorally effective; tests cannot
encode the desired behavior. Documentation HEALTH belongs to the judgment layer — periodic doc
review and, above all, behavioral evidence from live runs (the RUN-ADHERENCE-AUDIT instrument;
Run-2's defect distribution — seats signing DONE without reports despite duty-carrying docs — was
doc-health data the checker could never produce). Do not let a green run imply otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REGISTRY_REL = "operational/shared/blocks/registry.json"
OPERATIONAL_REL = "operational"

# The pre-registry marker the LR-13 splice used; migrated, must never reappear.
LEGACY_MARKER = "<!-- gate-output-contract (LR-13) -->"

_OPEN = re.compile(r"<!--\s*block:(?P<id>[A-Za-z0-9_+-]+)\s+v(?P<ver>\d+)\s*-->")


class RenderError(RuntimeError):
    """A render that cannot honor the registry aborts loudly — no partial landing."""


@dataclass(frozen=True)
class Defect:
    kind: str
    block: Optional[str]
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.message


def _close_marker(block_id: str) -> str:
    return f"<!-- /block:{block_id} -->"


def _open_marker(block_id: str, version: int) -> str:
    return f"<!-- block:{block_id} v{version} -->"


def load_registry(root: Path) -> dict:
    return json.loads((Path(root) / REGISTRY_REL).read_text(encoding="utf-8"))


def _source_path(root: Path, registry: dict, spec: dict, carrier: dict) -> Path:
    blocks_root = Path(root) / registry.get("blocks_root", "operational/shared/blocks")
    name = carrier.get("variant") or spec.get("source")
    if not name:
        raise RenderError(f"registry names no source for carrier {carrier.get('doc')!r}")
    return blocks_root / name


def _canonical_region(block_id: str, version: int, source_text: str) -> str:
    return (
        _open_marker(block_id, version)
        + "\n"
        + source_text.strip()
        + "\n"
        + _close_marker(block_id)
    )


def _find_region(text: str, block_id: str):
    """Locate the marker region for ``block_id``. Returns (start, end, version, body) or a
    string defect kind ('MISSING', 'DUPLICATE', 'UNCLOSED')."""
    opens = [
        m for m in _OPEN.finditer(text) if m.group("id") == block_id
    ]
    if not opens:
        return "MISSING"
    if len(opens) > 1:
        return "DUPLICATE"
    m = opens[0]
    close = text.find(_close_marker(block_id), m.end())
    if close == -1:
        return "UNCLOSED"
    body = text[m.end():close]
    end = close + len(_close_marker(block_id))
    return (m.start(), end, int(m.group("ver")), body)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def check(root: Path) -> list:
    """Walk the registry against the corpus under ``root``; return the typed-defect list.

    Empty list == mechanically conformant. NOT a statement of doc health — see module docstring.
    """
    root = Path(root)
    defects: list = []
    try:
        registry = load_registry(root)
    except (OSError, json.JSONDecodeError) as exc:
        return [Defect("BAD-REGISTRY", None, f"BAD-REGISTRY: {REGISTRY_REL}: {exc}")]

    registered_ids = set(registry.get("blocks", {}))

    # Required template files exist (a block's duty points at them). An entry is either a path
    # string or a NAMED-ADAPTATION dict ({path, adapts, level, reason}) — an adaptation must name
    # an existing base AND state its reason, or it is an unregistered fork.
    for entry in registry.get("templates", []):
        rel = entry if isinstance(entry, str) else entry.get("path", "")
        if not rel or not (root / rel).is_file():
            defects.append(
                Defect("MISSING-TEMPLATE", None, f"MISSING-TEMPLATE: {rel or entry!r} is registered but absent")
            )
        if isinstance(entry, dict):
            base = entry.get("adapts")
            if not base or not (root / base).is_file():
                defects.append(
                    Defect(
                        "BAD-REGISTRY",
                        None,
                        f"BAD-REGISTRY: template adaptation {rel} must name an existing base "
                        f"('adapts'); got {base!r}",
                    )
                )
            if not entry.get("reason"):
                defects.append(
                    Defect(
                        "BAD-REGISTRY",
                        None,
                        f"BAD-REGISTRY: template adaptation {rel} states no reason — an unreasoned "
                        f"adaptation is a fork",
                    )
                )

    for block_id, spec in registry.get("blocks", {}).items():
        version = int(spec["version"])
        for carrier in spec.get("carriers", []):
            doc_rel = carrier["doc"]
            doc = root / doc_rel
            if not doc.is_file():
                defects.append(
                    Defect("MISSING-DOC", block_id, f"MISSING-DOC: {doc_rel} (carrier of {block_id})")
                )
                continue
            try:
                source_text = _source_path(root, registry, spec, carrier).read_text(encoding="utf-8")
            except (OSError, RenderError):
                defects.append(
                    Defect(
                        "MISSING-SOURCE",
                        block_id,
                        f"MISSING-SOURCE: {block_id} source for {doc_rel} unreadable under "
                        f"{registry.get('blocks_root')}",
                    )
                )
                continue
            text = doc.read_text(encoding="utf-8")
            region = _find_region(text, block_id)
            if region == "MISSING":
                defects.append(
                    Defect(
                        "MISSING-BLOCK",
                        block_id,
                        f"MISSING-BLOCK: {doc_rel} is registered to carry {block_id} v{version} "
                        f"but has no marker — render it (tools/render_blocks.py --render)",
                    )
                )
                continue
            if region == "DUPLICATE":
                defects.append(
                    Defect("DUPLICATE-MARKER", block_id, f"DUPLICATE-MARKER: {block_id} twice in {doc_rel}")
                )
                continue
            if region == "UNCLOSED":
                defects.append(
                    Defect(
                        "UNCLOSED-MARKER",
                        block_id,
                        f"UNCLOSED-MARKER: {block_id} in {doc_rel} has no {_close_marker(block_id)}",
                    )
                )
                continue
            _start, _end, landed_ver, body = region
            if landed_ver != version:
                defects.append(
                    Defect(
                        "STALE-VERSION",
                        block_id,
                        f"STALE-VERSION: {doc_rel} carries {block_id} v{landed_ver}, registry says "
                        f"v{version} — re-render",
                    )
                )
                continue
            if body.strip() != source_text.strip():
                defects.append(
                    Defect(
                        "CONTENT-DRIFT",
                        block_id,
                        f"CONTENT-DRIFT: {doc_rel} in-marker content of {block_id} v{version} differs "
                        f"from its source — either heal the doc (--render) or change the SOURCE and "
                        f"bump the version (the source is the single place content lives)",
                    )
                )

    # Sweep the whole operational/ corpus for markers the registry doesn't know + legacy markers.
    registered_carriers = {
        (c["doc"], bid)
        for bid, spec in registry.get("blocks", {}).items()
        for c in spec.get("carriers", [])
    }
    for md in sorted((root / OPERATIONAL_REL).rglob("*.md")):
        rel = md.relative_to(root).as_posix()
        text = md.read_text(encoding="utf-8")
        if LEGACY_MARKER in text:
            defects.append(
                Defect(
                    "LEGACY-MARKER",
                    "gate-output-contract",
                    f"LEGACY-MARKER: {rel} still carries the pre-registry marker {LEGACY_MARKER!r} "
                    f"— migrated 2026-06-12, must not reappear",
                )
            )
        for m in _OPEN.finditer(text):
            bid = m.group("id")
            if bid not in registered_ids:
                defects.append(
                    Defect(
                        "UNKNOWN-MARKER",
                        bid,
                        f"UNKNOWN-MARKER: {rel} carries block marker {bid!r} the registry doesn't "
                        f"know — register it or remove it",
                    )
                )
            elif (rel, bid) not in registered_carriers:
                defects.append(
                    Defect(
                        "UNKNOWN-MARKER",
                        bid,
                        f"UNKNOWN-MARKER: {rel} carries {bid!r} but is not a registered carrier of it",
                    )
                )
    return defects


def landed_block_body(root: Path, doc_rel: str, block_id: str) -> str:
    """The in-marker body of ``block_id`` as landed in ``doc_rel`` (test/audit convenience)."""
    text = (Path(root) / doc_rel).read_text(encoding="utf-8")
    region = _find_region(text, block_id)
    if isinstance(region, str):
        raise RenderError(f"{doc_rel}: {block_id} region not landed cleanly ({region})")
    return region[3]


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def render(root: Path) -> list:
    """Render every registered carrier from its source. Returns the list of actions performed
    (empty == already conformant). Heals drift + stale versions in place; splices a not-yet-landed
    block immediately BEFORE its registered anchor. Touches NOTHING outside marker regions."""
    root = Path(root)
    registry = load_registry(root)
    actions: list = []
    for block_id, spec in registry.get("blocks", {}).items():
        version = int(spec["version"])
        for carrier in spec.get("carriers", []):
            doc_rel = carrier["doc"]
            doc = root / doc_rel
            if not doc.is_file():
                raise RenderError(f"{doc_rel}: carrier doc missing — aborting (no partial landing)")
            source_text = _source_path(root, registry, spec, carrier).read_text(encoding="utf-8")
            canonical = _canonical_region(block_id, version, source_text)
            text = doc.read_text(encoding="utf-8")
            region = _find_region(text, block_id)
            if region in ("DUPLICATE", "UNCLOSED"):
                raise RenderError(
                    f"{doc_rel}: {block_id} marker region is {region} — fix by hand before rendering"
                )
            if region == "MISSING":
                anchor = carrier.get("anchor")
                if not anchor:
                    raise RenderError(f"{doc_rel}: {block_id} not landed and no anchor registered")
                if text.count(anchor) != 1:
                    raise RenderError(
                        f"{doc_rel}: anchor {anchor!r} for {block_id} found "
                        f"{text.count(anchor)} times — aborting (no partial landing)"
                    )
                doc.write_text(text.replace(anchor, canonical + "\n\n" + anchor, 1), encoding="utf-8")
                actions.append(f"spliced {block_id} v{version} into {doc_rel} before {anchor!r}")
                continue
            start, end, landed_ver, body = region
            if landed_ver == version and body.strip() == source_text.strip():
                continue  # idempotent: already canonical
            doc.write_text(text[:start] + canonical + text[end:], encoding="utf-8")
            actions.append(f"re-rendered {block_id} v{version} in {doc_rel} (was v{landed_ver})")
    return actions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--render", action="store_true", help="render/splice (default: check only)")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repo root (default: this file's repo)",
    )
    args = parser.parse_args(argv)
    if args.render:
        try:
            actions = render(args.root)
        except RenderError as exc:
            print(f"ABORT: {exc}", file=sys.stderr)
            return 2
        for a in actions:
            print(a)
        if not actions:
            print("already conformant — nothing to render")
    defects = check(args.root)
    for d in defects:
        print(d.message, file=sys.stderr)
    if defects:
        print(f"{len(defects)} doc-system defect(s).", file=sys.stderr)
        return 1
    print("doc-system check: green (mechanical conformance only — health is the judgment layer's)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
