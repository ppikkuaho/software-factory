#!/usr/bin/env python3
"""Link pinned Codex auth to the machine's single default token lineage."""

from __future__ import annotations

import filecmp
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepairResult:
    changed: bool
    pinned_auth_path: Path
    target_auth_path: Path
    backup_path: Path | None


def _resolved_link_target(link: Path) -> Path:
    target = Path(os.readlink(link))
    if not target.is_absolute():
        target = link.parent / target
    return target.resolve(strict=False)


def _backup_regular_auth(pinned_auth: Path, backup_root: Path) -> Path:
    stat_result = pinned_auth.stat()
    backup_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(backup_root, 0o700)
    backup = backup_root / f"auth.json.pre-link-{stat_result.st_mtime_ns}"
    if backup.exists():
        if not backup.is_file() or not filecmp.cmp(
            pinned_auth,
            backup,
            shallow=False,
        ):
            raise RuntimeError(
                f"refusing to replace non-matching existing auth backup at {backup}"
            )
    else:
        shutil.copy2(pinned_auth, backup)
    os.chmod(backup, 0o600)
    return backup


def repair_pinned_codex_auth(
    *,
    repo_root: Path | None = None,
    user_home: Path | None = None,
) -> RepairResult:
    """Back up any pinned copy, then atomically install the one canonical link."""
    root = (
        Path(__file__).resolve().parents[1]
        if repo_root is None
        else Path(repo_root).resolve()
    )
    home = Path.home() if user_home is None else Path(user_home).resolve()
    target_auth = (home / ".codex" / "auth.json").absolute()
    pinned_auth = root / ".codex-pinned" / "config" / "auth.json"

    if not target_auth.is_file():
        raise RuntimeError(
            "default Codex auth is missing at ~/.codex/auth.json; run codex login "
            "against the default ~/.codex home, then rerun this repair"
        )

    target_resolved = target_auth.resolve()
    if (
        pinned_auth.is_symlink()
        and _resolved_link_target(pinned_auth) == target_resolved
    ):
        return RepairResult(
            changed=False,
            pinned_auth_path=pinned_auth,
            target_auth_path=target_auth,
            backup_path=None,
        )

    pinned_auth.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    backup = None
    if pinned_auth.exists() and not pinned_auth.is_symlink():
        if not pinned_auth.is_file():
            raise RuntimeError(
                f"refusing to replace non-file pinned auth path at {pinned_auth}"
            )
        backup = _backup_regular_auth(
            pinned_auth,
            root / ".codex-pinned" / "auth-backups",
        )

    temporary_link = pinned_auth.with_name(
        f".auth.json.link-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        temporary_link.symlink_to(target_auth)
        os.replace(temporary_link, pinned_auth)
    finally:
        try:
            temporary_link.unlink()
        except FileNotFoundError:
            pass

    return RepairResult(
        changed=True,
        pinned_auth_path=pinned_auth,
        target_auth_path=target_auth,
        backup_path=backup,
    )


def main() -> int:
    try:
        result = repair_pinned_codex_auth()
    except RuntimeError as exc:
        print(f"Codex auth link repair refused: {exc}")
        return 1
    if result.changed:
        backup = f"; backup={result.backup_path}" if result.backup_path else ""
        print(
            f"Codex auth linked: {result.pinned_auth_path} -> "
            f"{result.target_auth_path}{backup}"
        )
    else:
        print(
            f"Codex auth link already correct: {result.pinned_auth_path} -> "
            f"{result.target_auth_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
