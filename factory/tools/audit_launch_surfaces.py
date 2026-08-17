"""Report generated launch-surface block inventory by role.

This is an operator/design audit helper. It does not spawn agents and does not
write runtime state. The editable source of truth remains the canonical
role/config/template files carrying ``surface:<role>`` blocks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harnessd.spawn import launch_surface


DEFAULT_ROLES = ("L1", "L2", "L2+", "L3", "L3+", "L4", "L4+", "L5", "L5+")


def _source_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_audit(*, roles: tuple[str, ...] = DEFAULT_ROLES, root: Path | None = None) -> dict[str, Any]:
    root = root or launch_surface.harness_root()
    report: dict[str, Any] = {
        "schema_version": 1,
        "roles": {},
    }
    for role in roles:
        launch_surface.validate(role, root=root)
        blocks = launch_surface.surface_blocks(role, root=root)
        sources = launch_surface.source_files(role)
        role_report: dict[str, Any] = {
            "source_files": [
                {
                    "path": rel,
                    "bytes": (root / rel).stat().st_size,
                    "sha256": _source_sha(root / rel),
                }
                for rel in sources
            ],
            "kinds": {},
        }
        for kind in ("launch", "reference", "hidden"):
            kind_blocks = [block for block in blocks if block.kind == kind]
            role_report["kinds"][kind] = {
                "block_count": len(kind_blocks),
                "bytes": sum(len(block.body.encode("utf-8")) for block in kind_blocks),
                "blocks": [
                    {
                        "id": block.id,
                        "source": block.source,
                        "bytes": len(block.body.encode("utf-8")),
                    }
                    for block in kind_blocks
                ],
            }
        report["roles"][role] = role_report
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit launch-surface source blocks by role.")
    parser.add_argument("--role", action="append", dest="roles", help="Role to audit; repeatable.")
    parser.add_argument("--root", type=Path, default=None, help="Harness root; defaults to repo root.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    roles = tuple(args.roles) if args.roles else DEFAULT_ROLES
    payload = build_audit(roles=roles, root=args.root)
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))


if __name__ == "__main__":
    main()
