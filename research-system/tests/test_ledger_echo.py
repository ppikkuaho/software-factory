"""`ht ledger echo` (Phase 2) — verifier-authored support echoes (A1 §7).

Each call appends exactly one traceable {source_ref, epoch} and increments
support_count by exactly 1 (never a bare counter). Rejections: non-verifier role,
unknown entry, missing source-ref.
"""

from __future__ import annotations

import json

import pytest

from conftest import Sandbox
from ht import authority, cli


def _make_entry(sb: Sandbox) -> str:
    """Create a research-section ledger entry (verifier) to echo against."""
    r = sb.run(
        "ledger", "create", "--section", "research", "--book", "L4", "--text",
        "recurring orientation waste on L4", role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert r.returncode == 0, r.stderr
    return "L-1"


def test_echo_appends_traceable_and_increments_by_one(sandbox: Sandbox):
    sb = sandbox
    entry_id = _make_entry(sb)
    assert sb.load("ledger/L4/research/L-1.json")["support_count"] == 0

    r = sb.run("ledger", "echo", "--entry", entry_id, "--source-ref",
               "observation#O-12", role="verifier", env_extra={"HT_LANE": "L4"})
    assert r.returncode == 0, r.stderr
    e = sb.load("ledger/L4/research/L-1.json")
    assert e["support_count"] == 1
    assert e["echoes"] == [{"source_ref": "observation#O-12", "epoch": 0}]

    # a second echo -> exactly +1 again, both increments traceable
    r = sb.run("ledger", "echo", "--entry", entry_id, "--source-ref",
               "observation#O-19", "--epoch", "0", role="verifier",
               env_extra={"HT_LANE": "L4"})
    assert r.returncode == 0, r.stderr
    e = sb.load("ledger/L4/research/L-1.json")
    assert e["support_count"] == 2
    assert [x["source_ref"] for x in e["echoes"]] == ["observation#O-12", "observation#O-19"]
    assert e["dedup_log"] == []  # legacy echo remains valid without dedup flags


def test_echo_and_dedup_land_atomically_in_one_commit(sandbox: Sandbox):
    sb = sandbox
    entry_id = _make_entry(sb)
    r = sb.run(
        "ledger", "create", "--section", "research", "--book", "L4", "--text",
        "second candidate for dedup", role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert r.returncode == 0, r.stderr

    before = sb.git("rev-parse", "HEAD").stdout.strip()
    r = sb.run(
        "ledger", "echo", "--entry", entry_id,
        "--source-ref", "observation#O-22",
        "--dedup-matched", "L-2", "--dedup-resolution", "distinct",
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert r.returncode == 0, r.stderr

    commit_count = sb.git("rev-list", "--count", f"{before}..HEAD")
    assert commit_count.returncode == 0, commit_count.stderr
    assert commit_count.stdout.strip() == "1"

    rel = "ledger/L4/research/L-1.json"
    old = json.loads(sb.git("show", f"{before}:{rel}").stdout)
    new = json.loads(sb.git("show", f"HEAD:{rel}").stdout)
    assert new["support_count"] == old["support_count"] + 1
    assert new["echoes"] == old["echoes"] + [
        {"source_ref": "observation#O-22", "epoch": 0}
    ]
    assert new["dedup_log"] == old["dedup_log"] + [
        {"matched": "L-2", "resolution": "distinct"}
    ]

    changed = sb.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    assert changed.returncode == 0, changed.stderr
    assert changed.stdout.splitlines() == [rel]


@pytest.mark.parametrize(
    "extra_args",
    [
        ("--dedup-matched", "L-2"),
        ("--dedup-resolution", "distinct"),
    ],
)
def test_echo_dedup_flags_must_be_supplied_together(
    sandbox: Sandbox, extra_args: tuple[str, str]
):
    sb = sandbox
    _make_entry(sb)
    r = sb.run(
        "ledger", "echo", "--entry", "L-1",
        "--source-ref", "observation#O-23", *extra_args,
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert r.returncode != 0
    assert "--dedup-matched" in r.stderr
    assert "--dedup-resolution" in r.stderr
    assert "together" in r.stderr


@pytest.mark.parametrize("matched", ["not-an-entry-id", "L-x", "L-99"])
def test_echo_dedup_matched_must_name_an_existing_entry(
    sandbox: Sandbox, matched: str
):
    sb = sandbox
    _make_entry(sb)
    r = sb.run(
        "ledger", "echo", "--entry", "L-1",
        "--source-ref", "observation#O-23",
        "--dedup-matched", matched, "--dedup-resolution", "distinct",
        role="verifier", env_extra={"HT_LANE": "L4"},
    )
    assert r.returncode != 0
    assert "dedup-matched" in r.stderr or "no such ledger entry" in r.stderr


def test_verifier_commit_has_lane_trailer_when_lane_is_set(sandbox: Sandbox):
    sb = sandbox
    _make_entry(sb)
    r = sb.run(
        "ledger", "echo", "--entry", "L-1",
        "--source-ref", "observation#O-24", role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert r.returncode == 0, r.stderr
    body = sb.git("show", "-s", "--format=%B", "HEAD").stdout
    assert "HT-Lane: L4" in body.splitlines()


def test_unassigned_verifier_echo_is_rejected(sandbox: Sandbox):
    sb = sandbox
    _make_entry(sb)
    r = sb.run(
        "ledger", "echo", "--entry", "L-1",
        "--source-ref", "observation#O-25", role="verifier",
    )
    assert r.returncode != 0
    assert "unassigned" in r.stderr and "book-scoped" in r.stderr


@pytest.mark.parametrize("raw_lane", ["", "   \t  "])
def test_empty_or_whitespace_lane_is_unassigned_and_rejected(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, raw_lane: str
):
    sb = sandbox
    _make_entry(sb)
    r = sb.run(
        "ledger", "echo", "--entry", "L-1",
        "--source-ref", "observation#O-26", role="verifier",
        env_extra={"HT_LANE": raw_lane},
    )
    assert r.returncode != 0
    assert "unassigned" in r.stderr and "book-scoped" in r.stderr

    monkeypatch.setenv("HT_ROLE", "verifier")
    monkeypatch.setenv("HT_LANE", raw_lane)
    _, lane = cli._require_role()
    assert lane is authority.UNASSIGNED_LANE


def test_lane_is_stripped_for_authority_and_commit_trailer(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
):
    sb = sandbox
    _make_entry(sb)
    r = sb.run(
        "ledger", "echo", "--entry", "L-1",
        "--source-ref", "observation#O-27", role="verifier",
        env_extra={"HT_LANE": "  L4\t"},
    )
    assert r.returncode == 0, r.stderr
    body = sb.git("show", "-s", "--format=%B", "HEAD").stdout
    assert "HT-Lane: L4" in body.splitlines()
    assert not any("  L4" in line for line in body.splitlines())

    monkeypatch.setenv("HT_ROLE", "verifier")
    monkeypatch.setenv("HT_LANE", "  L4\t")
    _, lane = cli._require_role()
    assert lane == "L4"


def test_echo_by_non_verifier_rejected(sandbox: Sandbox):
    sb = sandbox
    _make_entry(sb)
    r = sb.run("ledger", "echo", "--entry", "L-1", "--source-ref", "observation#O-1",
               role="director")
    assert r.returncode != 0
    # echoes + support_count are verifier-authored; director does not own them
    assert "director" in r.stderr and "verifier" in r.stderr and "A1 §10" in r.stderr


def test_echo_unknown_entry_rejected(sandbox: Sandbox):
    sb = sandbox
    r = sb.run("ledger", "echo", "--entry", "L-99", "--source-ref", "observation#O-1",
               role="verifier", env_extra={"HT_LANE": "L4"})
    assert r.returncode != 0
    assert "no such ledger entry" in r.stderr and "L-99" in r.stderr


def test_echo_missing_source_ref_rejected(sandbox: Sandbox):
    sb = sandbox
    _make_entry(sb)
    r = sb.run(
        "ledger", "echo", "--entry", "L-1", "--source-ref", "", role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert r.returncode != 0
    assert "source-ref" in r.stderr and "A1 §7" in r.stderr
