#!/usr/bin/env python3
"""Render and check versioned shared-duty blocks in role packets."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "MANIFEST.json"
START = re.compile(r"<!-- BEGIN BLOCK (?P<name>[a-z0-9-]+) (?P<version>v[0-9]+) -->")
END = re.compile(r"<!-- END BLOCK (?P<name>[a-z0-9-]+) (?P<version>v[0-9]+) -->")


class RenderError(Exception):
    """A role-packet block declaration or rendered region is invalid."""


@dataclass(frozen=True)
class Region:
    name: str
    version: str
    body_start: int
    body_end: int

    @property
    def key(self) -> tuple[str, str]:
        return self.name, self.version


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_bytes().decode("utf-8"))


def _regions(text: str, packet: Path) -> list[Region]:
    regions: list[Region] = []
    active: tuple[str, str, int] | None = None
    offset = 0

    for line in text.splitlines(keepends=True):
        marker = line.rstrip("\r\n")
        start = START.fullmatch(marker)
        end = END.fullmatch(marker)

        if marker.startswith("<!-- BEGIN BLOCK") and start is None:
            raise RenderError(f"{packet}: malformed BEGIN BLOCK marker: {marker}")
        if marker.startswith("<!-- END BLOCK") and end is None:
            raise RenderError(f"{packet}: malformed END BLOCK marker: {marker}")

        if start:
            if active is not None:
                raise RenderError(f"{packet}: nested block region at {marker}")
            active = (start["name"], start["version"], offset + len(line))
        elif end:
            if active is None:
                raise RenderError(f"{packet}: END BLOCK without BEGIN: {marker}")
            name, version, body_start = active
            if (end["name"], end["version"]) != (name, version):
                raise RenderError(
                    f"{packet}: closing marker {end['name']} {end['version']} "
                    f"does not match {name} {version}"
                )
            regions.append(Region(name, version, body_start, offset))
            active = None

        offset += len(line)

    if active is not None:
        name, version, _ = active
        raise RenderError(f"{packet}: unclosed block region {name} {version}")

    keys = [region.key for region in regions]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise RenderError(f"{packet}: duplicate block regions: {duplicates}")
    return regions


def _v1_packets(manifest: dict) -> list[dict]:
    return [packet for packet in manifest["packets"] if packet["version"] == "v1"]


def _source_for(manifest: dict, declaration: dict, role: str) -> Path:
    name = declaration["name"]
    version = declaration["version"]
    variant = declaration.get("variant")
    registry = manifest["block_registry"][name]

    if registry["version"] != version:
        raise RenderError(
            f"{role}: {name} declares {version}, registry has {registry['version']}"
        )

    carriers = [carrier for carrier in registry["carriers"] if carrier["role"] == role]
    if len(carriers) != 1:
        raise RenderError(f"{role}: {name} must have exactly one registry carrier")
    if carriers[0].get("variant") != variant:
        raise RenderError(
            f"{role}: {name} variant {variant!r} disagrees with registry "
            f"{carriers[0].get('variant')!r}"
        )

    if variant is None:
        relative = registry["source"]
    else:
        try:
            relative = registry["variants"][variant]
        except KeyError as exc:
            raise RenderError(f"{role}: {name} has unknown variant {variant!r}") from exc
    source = ROOT / relative
    if not source.is_file():
        raise RenderError(f"{role}: block source does not exist: {source}")
    return source


def _validate_registry_coverage(manifest: dict) -> None:
    declared: dict[str, set[tuple[str, str | None]]] = {
        name: set() for name in manifest["block_registry"]
    }
    for packet in _v1_packets(manifest):
        for block in packet["blocks"]:
            name = block["name"]
            if name not in declared:
                raise RenderError(f"{packet['role']}: undeclared registry block {name}")
            declared[name].add((packet["role"], block.get("variant")))

    for name, registry in manifest["block_registry"].items():
        carriers = {(carrier["role"], carrier.get("variant")) for carrier in registry["carriers"]}
        if declared[name] != carriers:
            raise RenderError(
                f"{name}: packet declarations {sorted(declared[name])} do not match "
                f"registry carriers {sorted(carriers)}"
            )


def render(*, check: bool) -> list[Path]:
    manifest = _load_manifest()
    _validate_registry_coverage(manifest)
    rewrites: list[tuple[Path, str]] = []
    drift: list[str] = []

    for packet_entry in _v1_packets(manifest):
        packet = ROOT / packet_entry["file"]
        if not packet.is_file():
            raise RenderError(f"packet does not exist: {packet}")
        text = packet.read_bytes().decode("utf-8")
        regions = _regions(text, packet)
        declarations = packet_entry["blocks"]
        expected = [(block["name"], block["version"]) for block in declarations]
        actual = [region.key for region in regions]
        if actual != expected:
            raise RenderError(
                f"{packet}: rendered regions {actual} do not exactly match declarations {expected}"
            )

        replacements: list[tuple[int, int, str]] = []
        for declaration, region in zip(declarations, regions, strict=True):
            source = _source_for(manifest, declaration, packet_entry["role"])
            source_text = source.read_bytes().decode("utf-8")
            rendered = text[region.body_start : region.body_end]
            if rendered != source_text:
                drift.append(
                    f"{packet.name}: {region.name} {region.version} differs from {source.name}"
                )
                replacements.append((region.body_start, region.body_end, source_text))

        if replacements:
            updated = text
            for start, end, source_text in reversed(replacements):
                updated = updated[:start] + source_text + updated[end:]
            rewrites.append((packet, updated))

    if check and drift:
        raise RenderError("rendered block drift:\n" + "\n".join(drift))
    if not check:
        for packet, updated in rewrites:
            packet.write_bytes(updated.encode("utf-8"))
    return [packet for packet, _ in rewrites]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--render", action="store_true", help="replace regions from sources")
    mode.add_argument("--check", action="store_true", help="fail if any region has drifted")
    args = parser.parse_args(argv)

    try:
        changed = render(check=args.check)
    except (KeyError, json.JSONDecodeError, RenderError) as exc:
        print(f"role-blocks: ERROR: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print("role-blocks: OK")
    elif changed:
        print("role-blocks: rendered " + ", ".join(path.name for path in changed))
    else:
        print("role-blocks: already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
