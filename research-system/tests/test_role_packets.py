from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "system" / "roles"
MANIFEST = json.loads((ROLES / "MANIFEST.json").read_text(encoding="utf-8"))
V1 = {packet["role"]: packet for packet in MANIFEST["packets"] if packet["version"] == "v1"}

EXPECTED_SECTIONS = [
    "1. Role & purpose",
    "2. Position",
    "3. The work",
    "4. Boundaries with rationale",
    "5. Calibration",
]

V0_HISTORY = [
    {
        "role": "senior",
        "version": "v0-draft",
        "file": "senior.v0-draft.md",
        "sha256": "2177d383d555fcdd674f6873d6f40aaf56360966e5c6a34632d5617281e53f41",
        "bytes": 3756,
    },
    {
        "role": "junior",
        "version": "v0-draft",
        "file": "junior.v0-draft.md",
        "sha256": "2d392b6dde7f06a28159fe5bc705e5fe5e6980c074a0461693c5f9e1ffb66b34",
        "bytes": 2872,
    },
    {
        "role": "checker",
        "version": "v0-draft",
        "file": "checker.v0-draft.md",
        "sha256": "3a758285c1be7b3637bba8b94b1be80a9ff5049ce63311963dba90ef0b2ff320",
        "bytes": 3119,
    },
    {
        "role": "verifier",
        "version": "v0-draft",
        "file": "verifier.v0-draft.md",
        "sha256": "09f1b07688a8499551eccd677fd52f22e98facc550d0a1e63c28497b50b77e50",
        "bytes": 5141,
    },
]

START = re.compile(r"^<!-- BEGIN BLOCK ([a-z0-9-]+) (v[0-9]+) -->$", re.MULTILINE)


def _packet_text(role: str) -> str:
    return (ROLES / V1[role]["file"]).read_text(encoding="utf-8")


def _body_without_frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    _, _, body = text.split("---\n", 2)
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def _rendered_regions(text: str) -> list[tuple[str, str, str]]:
    regions = []
    for start in START.finditer(text):
        name, version = start.groups()
        body_start = start.end() + 1
        closing = f"<!-- END BLOCK {name} {version} -->"
        body_end = text.index(closing, body_start)
        regions.append((name, version, text[body_start:body_end]))
    return regions


def _block_source(declaration: dict, role: str) -> Path:
    registry = MANIFEST["block_registry"][declaration["name"]]
    variant = declaration.get("variant")
    if variant is None:
        return ROLES / registry["source"]
    return ROLES / registry["variants"][variant]


@pytest.mark.parametrize("role", sorted(V1))
def test_packet_has_exact_p9_structure(role: str) -> None:
    text = _packet_text(role)
    frontmatter = text.split("---\n", 2)[1]
    headings = re.findall(r"^## ([1-5]\..+)$", text, flags=re.MULTILINE)
    assert headings == EXPECTED_SECTIONS
    assert "status: v1 — ratified 2026-07-14" in frontmatter
    assert "This packet is self-contained for the role" in text
    assert "Forbidden moves" not in text


@pytest.mark.parametrize("role", sorted(V1))
def test_design_citations_are_frontmatter_only(role: str) -> None:
    text = _packet_text(role)
    frontmatter = text.split("---\n", 2)[1]
    body = _body_without_frontmatter(text)
    assert "provenance:" in frontmatter
    assert not re.search(
        r"\b(?:concept|A1|A3|B4|macro|PC|coherence)\s+§",
        body,
        flags=re.IGNORECASE,
    )
    assert not re.search(r"\bDP-A[0-9]+-", body)
    assert not re.search(r"\bR-i[0-9]+-", body, flags=re.IGNORECASE)
    assert not re.search(r"\bF1[01]\b", body)


@pytest.mark.parametrize(
    "inline_citation",
    ["macro §3", "PC §5", "coherence §12", "R-i6-2", "F10", "F11"],
)
def test_pc_citation_lint_detects_item6_inline_vocabulary(
    inline_citation: str,
) -> None:
    body = _body_without_frontmatter(_packet_text("principal-coordinator"))
    mutated = body + f"\nInline design citation: {inline_citation}.\n"
    pattern = re.compile(
        r"\b(?:macro|PC|coherence)\s+§|\bR-i[0-9]+-|\bF1[01]\b",
        flags=re.IGNORECASE,
    )
    assert pattern.search(mutated)


def test_drift_check_makes_the_maintenance_ruling_mechanical(tmp_path: Path) -> None:
    """The user's RQ-4 'maintenance nightmare' ruling made machinery."""
    copied = tmp_path / "roles"
    shutil.copytree(ROLES, copied)
    packet = copied / "senior.v1.md"
    original = packet.read_text(encoding="utf-8")
    packet.write_text(original.replace("A negative result is a real result", "DRIFT", 1))

    check = subprocess.run(
        [sys.executable, str(copied / "render.py"), "--check"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert check.returncode == 1
    assert "rendered block drift" in check.stderr

    rendered = subprocess.run(
        [sys.executable, str(copied / "render.py"), "--render"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert rendered.returncode == 0
    assert "senior.v1.md" in rendered.stdout
    first_bytes = packet.read_bytes()

    rendered_again = subprocess.run(
        [sys.executable, str(copied / "render.py"), "--render"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert rendered_again.returncode == 0
    assert "already current" in rendered_again.stdout
    assert packet.read_bytes() == first_bytes


def test_drift_check_is_byte_exact_across_line_endings(tmp_path: Path) -> None:
    copied = tmp_path / "roles"
    shutil.copytree(ROLES, copied)
    source = copied / "blocks" / "negative-result-is-real.v1.md"
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))

    check = subprocess.run(
        [sys.executable, str(copied / "render.py"), "--check"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert check.returncode == 1
    assert "rendered block drift" in check.stderr


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_manifest_and_rendered_regions_fail_both_drift_directions(
    tmp_path: Path, mutation: str
) -> None:
    copied = tmp_path / "roles"
    shutil.copytree(ROLES, copied)
    packet = copied / "senior.v1.md"
    text = packet.read_text(encoding="utf-8")
    if mutation == "missing":
        text = re.sub(
            r"<!-- BEGIN BLOCK completion-reporting-honesty v1 -->\n.*?"
            r"<!-- END BLOCK completion-reporting-honesty v1 -->\n",
            "",
            text,
            count=1,
            flags=re.DOTALL,
        )
    else:
        text += (
            "\n<!-- BEGIN BLOCK undeclared-test-block v1 -->\n"
            "not declared\n"
            "<!-- END BLOCK undeclared-test-block v1 -->\n"
        )
    packet.write_text(text, encoding="utf-8")

    check = subprocess.run(
        [sys.executable, str(copied / "render.py"), "--check"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert check.returncode == 1
    assert "do not exactly match declarations" in check.stderr


def test_declared_blocks_are_exactly_rendered_from_sources() -> None:
    observed_carriers: dict[str, set[tuple[str, str | None]]] = {
        name: set() for name in MANIFEST["block_registry"]
    }

    for role, packet in V1.items():
        declarations = packet["blocks"]
        regions = _rendered_regions(_packet_text(role))
        assert [(name, version) for name, version, _ in regions] == [
            (block["name"], block["version"]) for block in declarations
        ]
        for declaration, (_, _, rendered) in zip(declarations, regions, strict=True):
            source = _block_source(declaration, role).read_bytes()
            assert rendered.encode("utf-8") == source
            observed_carriers[declaration["name"]].add(
                (role, declaration.get("variant"))
            )

    for name, registry in MANIFEST["block_registry"].items():
        expected = {
            (carrier["role"], carrier.get("variant")) for carrier in registry["carriers"]
        }
        assert observed_carriers[name] == expected


def test_firm_block_carrier_sets_and_verifier_variant() -> None:
    all_roles = {"senior", "junior", "checker", "verifier"}
    registry = MANIFEST["block_registry"]
    assert {carrier["role"] for carrier in registry["negative-result-is-real"]["carriers"]} == all_roles
    assert {
        carrier["role"]
        for carrier in registry["no-claim-inflation-never-iterate-to-positive"]["carriers"]
    } == all_roles
    assert {
        carrier["role"] for carrier in registry["completion-reporting-honesty"]["carriers"]
    } == {"senior", "junior"}
    authority = registry["unit-never-writes-tree-or-ledger"]
    assert {(carrier["role"], carrier.get("variant")) for carrier in authority["carriers"]} == {
        ("senior", None),
        ("junior", None),
        ("checker", None),
        ("verifier", "verifier-counterpart"),
    }
    assert "variant_reason" in authority


def test_v0_files_and_manifest_history_are_untouched() -> None:
    assert MANIFEST["$comment"] == (
        "Role-packet manifest (A3 §1/§2 — role packets are versioned + hashed; the hash is "
        "what dispatch records bind to via A1 §3 role_packet {version, hash} in Phase 3 "
        "runtime). v0-draft: DOCUMENTS ONLY — no dispatch has run under any packet; not "
        "executable."
    )
    assert MANIFEST["provenance"] == "director-provisional-2026-07-07"
    assert MANIFEST["packets"][:4] == V0_HISTORY
    for entry in V0_HISTORY:
        data = (ROLES / entry["file"]).read_bytes()
        assert len(data) == entry["bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]


def test_v1_manifest_hashes_match_packet_bytes() -> None:
    for packet in V1.values():
        data = (ROLES / packet["file"]).read_bytes()
        assert len(data) == packet["bytes"]
        assert hashlib.sha256(data).hexdigest() == packet["sha256"]


def test_verifier_procedure_is_derived_traceable_and_referenced() -> None:
    procedure_path = ROLES / "verifier-procedure.v1.md"
    procedure = procedure_path.read_text(encoding="utf-8")
    frontmatter, body = procedure.split("---\n", 2)[1:]
    assert "status: derived" in frontmatter
    assert "derived_from: system/notes/RESEARCH-SYSTEM-VERIFIER-PROTOCOL-2026-07-07.md" in frontmatter
    assert "B4-adjacent ruling changes trigger re-derivation of this doc." in body
    assert re.findall(r"^## (VP-[0-9]{2})", body, flags=re.MULTILINE) == [
        f"VP-{index:02d}" for index in range(1, 15)
    ]
    expected_map = {
        "VP-01": "B4 §1",
        "VP-02": "B4 §2.1, B4 §9",
        "VP-03": "B4 §2.2",
        "VP-04": "B4 §2.3, B4 §4",
        "VP-05": "B4 §2.4, B4 §3, B4 §7.2",
        "VP-06": "B4 §2.5",
        "VP-07": "B4 §2.6, B4 §5",
        "VP-08": "B4 §4",
        "VP-09": "B4 §2.7, B4 §10",
        "VP-10": "B4 §6",
        "VP-11": "B4 §7, B4 §5",
        "VP-12": "B4 §8",
        "VP-13": "B4 §9",
        "VP-14": "B4 §11, B4 §12",
    }
    for step, source in expected_map.items():
        assert f"  {step}: {source}" in frontmatter
    assert "preponderance of granted evidence" in body
    assert "Never shape the unit's plan or dispatch" in body

    verifier = _body_without_frontmatter(_packet_text("verifier"))
    assert "For each submitted report, follow `verifier-procedure.v1.md`" in verifier
    for inlined_sequence_term in (
        "sampled-blind re-application",
        "support count from traceable echoes",
        "watch-verdict mapping",
        "merge gate",
    ):
        assert inlined_sequence_term not in verifier


@pytest.mark.parametrize("role", sorted(V1))
def test_packets_do_not_import_harness_specific_machinery(role: str) -> None:
    body = _body_without_frontmatter(_packet_text(role))
    forbidden = (
        "trace stanza",
        "requirement ID",
        "RTM",
        "two-model",
        "gate-output-contract",
        "E2",
        ".signal.",
        "surface:",
    )
    for term in forbidden:
        assert term.lower() not in body.lower()
