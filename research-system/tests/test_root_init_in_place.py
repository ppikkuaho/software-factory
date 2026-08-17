"""In-place genesis: `ht root init` run inside an existing research root.

The observed defect (walking-skeleton F1/F2, 2026-07-16): on a fresh clone of
the research root, `ht root init` crashed with shutil.SameFileError in the
schemas copy loop (src == dst) BEFORE writing the tier1 .gitkeep markers and
the scaffold commit — so no public command could ever mint the first issue
(`issue mint` requires tier1/issues in committed HEAD; the pre-commit hook
rejects out-of-band seeding). These tests walk in the front door.
"""

from __future__ import annotations

from conftest import Sandbox


def test_root_init_in_place_with_own_schemas_dir(sandbox: Sandbox) -> None:
    # Re-run root init pointing --schemas at the root's OWN schemas dir:
    # every copy is src == dst, the exact in-place-genesis condition.
    r = sandbox.run(
        "root", "init", "--schemas", str(sandbox.root / "system" / "schemas"),
        role="harness",
    )
    assert r.returncode == 0, f"in-place root init crashed: {r.stderr}\n{r.stdout}"
    # scaffold is intact and committed
    assert (sandbox.root / "tier1" / "issues" / ".gitkeep").exists()
    schemas = list((sandbox.root / "system" / "schemas").glob("*.json"))
    assert schemas, "schemas dir emptied by in-place re-init"


def test_root_init_in_place_is_idempotent(sandbox: Sandbox) -> None:
    own = str(sandbox.root / "system" / "schemas")
    for _ in range(2):
        r = sandbox.run("root", "init", "--schemas", own, role="harness")
        assert r.returncode == 0, r.stderr


def test_front_door_first_issue_after_in_place_reinit(sandbox: Sandbox) -> None:
    # The recipient path that was dead-locked: genesis -> seed -> first issue.
    r = sandbox.run(
        "root", "init", "--schemas", str(sandbox.root / "system" / "schemas"),
        role="harness",
    )
    assert r.returncode == 0, r.stderr
    r = sandbox.run(
        "ledger", "create", "--section", "user",
        "--text", "front-door seed", "--proposed-by", "user",
        role="user",
    )
    assert r.returncode == 0, f"ledger create failed: {r.stderr}"
    r = sandbox.run(
        "issue", "mint", "--title", "front door", "--question", "does it open?",
        "--done-definition", "issue exists in committed HEAD",
        "--provenance", "ledger#L-1", "--lanes", "L4",
        role="pc",
    )
    assert r.returncode == 0, f"first issue mint failed: {r.stderr}"
    assert (sandbox.root / "tier1" / "issues" / "I-1.json").exists()
