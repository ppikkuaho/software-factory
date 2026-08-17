"""Pre-commit backstop (0.D / A2 §4.2): the hook rejects out-of-band writes,
role-field violations on staged content, report modifications, and schema-invalid
staged state — even when ht is bypassed."""

from __future__ import annotations

import json

from conftest import Sandbox, seed_worked_node


def _edit_json(sb: Sandbox, rel: str, mutate) -> None:
    p = sb.root / rel
    d = json.loads(p.read_text())
    mutate(d)
    p.write_text(json.dumps(d))


def test_out_of_band_write_rejected(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")
    _edit_json(sb, "trees/L4/nodes/1/node.json", lambda d: d.update(premise="hand hacked"))
    assert sb.git("add", "trees/L4/nodes/1/node.json").returncode == 0
    # commit WITHOUT HT_COMMIT -> out-of-band
    r = sb.git("commit", "-m", "sneaky")
    assert r.returncode != 0
    assert "out-of-band" in r.stderr and "use ht" in r.stderr


def test_staged_write_with_nonowning_role_rejected(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")
    _edit_json(sb, "trees/L4/nodes/1/node.json", lambda d: d.update(premise="rewritten by verifier"))
    assert sb.git("add", "trees/L4/nodes/1/node.json").returncode == 0
    # HT_COMMIT set but verifier does not own node.premise (director's field)
    r = sb.git("commit", "-m", "verifier edits premise",
               env_extra={"HT_COMMIT": "1", "HT_ROLE": "verifier"})
    assert r.returncode != 0
    assert "premise" in r.stderr and "A1 §10" in r.stderr


def test_staged_schema_invalid_rejected(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")
    _edit_json(sb, "trees/L4/nodes/1/node.json", lambda d: d.update(status="active"))
    assert sb.git("add", "trees/L4/nodes/1/node.json").returncode == 0
    r = sb.git("commit", "-m", "invalid status",
               env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"})
    assert r.returncode != 0
    assert "schema" in r.stderr.lower()


def test_report_modification_rejected(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")  # submits d-1-1-report.md
    report = sb.root / "trees/L4/nodes/1/reports/d-1-1-report.md"
    report.write_text("# Tampered report\n")
    assert sb.git("add", str(report)).returncode == 0
    r = sb.git("commit", "-m", "tamper report",
               env_extra={"HT_COMMIT": "1", "HT_ROLE": "unit"})
    assert r.returncode != 0
    assert "report" in r.stderr and "frozen" in r.stderr


def test_legitimate_tool_commit_passes_hook(tree: Sandbox):
    # sanity: the hook does NOT block real ht commits (verified implicitly by seed,
    # asserted here on the commit count growing through a normal mutation)
    sb = tree
    before = sb.git("rev-list", "--count", "HEAD").stdout.strip()
    seed_worked_node(sb, "1")
    after = sb.git("rev-list", "--count", "HEAD").stdout.strip()
    assert int(after) > int(before)


# --- C1: mechanical status-flip carve-out requires a new granted claim ---------

def test_c1_bare_status_flip_without_claim_rejected(tree: Sandbox):
    """Reviewer probe (k): hand-edit status unexplored->worked with NO claims change,
    committed under HT_COMMIT=1 + HT_ROLE=verifier, must be rejected — the flip is a
    harness-class write only when the SAME diff lands a new granted claim."""
    sb = tree
    assert sb.run("node", "mint", "--tree", "L4", "--root", "--premise", "p",
                  "--rationale", "s", role="director").returncode == 0
    # node 1 is committed as unexplored with no claims; flip status only
    _edit_json(sb, "trees/L4/nodes/1/node.json", lambda d: d.update(status="worked"))
    assert sb.git("add", "trees/L4/nodes/1/node.json").returncode == 0
    r = sb.git("commit", "-m", "sneaky flip",
               env_extra={"HT_COMMIT": "1", "HT_ROLE": "verifier"})
    assert r.returncode != 0
    assert "status" in r.stderr and "A1 §10" in r.stderr


def test_c1_real_claim_grant_flip_passes_hook(tree: Sandbox):
    """Positive control: the genuine claim-grant (status flip + new granted claim in
    one commit, HT_ROLE=verifier) is admitted by the hook."""
    sb = tree
    before = sb.git("rev-list", "--count", "HEAD").stdout.strip()
    seed_worked_node(sb, "1")  # its claim-grant step commits through the hook
    assert sb.load("trees/L4/nodes/1/node.json")["status"] == "worked"
    after = sb.git("rev-list", "--count", "HEAD").stdout.strip()
    assert int(after) > int(before)


# --- C2: creation content is authority-checked against the creating role --------

def test_c2_handcrafted_rich_node_rejected(tree: Sandbox):
    """Reviewer C2 repro: a hand-crafted node.json carrying verifier-owned epistemic
    content (standing=supported + a granted tier-3 claim), added and committed under
    HT_ROLE=director, must be rejected — closing the 'create rich node then merge with
    no verifier' D10 bypass."""
    sb = tree
    node = {
        "id": "1", "parent": None, "premise": "smuggled premise",
        "minted_from": "user-direct", "supersedes": None, "superseded_by_node": None,
        "status": "worked", "status_reason": None, "conflicts": [], "git_binding": None,
        "standing": "supported", "standing_note": "forged",
        "claims": [{
            "id": "c-1-1", "node": "1", "source_dispatch": "d-1-1",
            "text": "forged tier-3", "proposed_tier": 3, "granted_tier": 3,
            "standing_class": "trunk", "revalidation": None,
            "anchors": [{"path": "x", "start_line": 1, "end_line": 2}],
            "epoch": 0, "instruments": [], "status": "granted",
        }],
        "measurements": [], "learnings_out": [],
    }
    sb.write_file("trees/L4/nodes/1/node.json", json.dumps(node))
    assert sb.git("add", "trees/L4/nodes/1/node.json").returncode == 0
    r = sb.git("commit", "-m", "forged node",
               env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"})
    assert r.returncode != 0
    assert ("standing" in r.stderr or "claims" in r.stderr) and "A1 §10" in r.stderr


def test_c2_mint_still_passes_hook(tree: Sandbox):
    """Positive control: a genuine `ht node mint` (director-owned nav fields + default
    epistemic values) is admitted at creation."""
    sb = tree
    r = sb.run("node", "mint", "--tree", "L4", "--root", "--premise", "real premise",
               "--rationale", "seed", role="director")
    assert r.returncode == 0, r.stderr
    node = sb.load("trees/L4/nodes/1/node.json")
    assert node["status"] == "unexplored" and node["standing"] == "untested"


# --- adjudication records: write-once + verifier-authored (director ruling) -----

def test_adjudication_write_once_and_verifier_stamp(tree: Sandbox):
    """Adjudication records are the verdict history the bounce loop escalates with
    (B4 §5) — tamper-evident, same argument as report freeze. Modifications of an
    existing record are rejected (write-once); the path class is verifier-authored;
    a verifier adding the next numbered record (-a2 beside -a1) passes."""
    sb = tree
    assert sb.run("node", "mint", "--tree", "L4", "--root", "--premise", "p",
                  "--rationale", "s", role="director").returncode == 0
    assert sb.run("dispatch", "create", "--node", "1", "--question", "q",
                  "--done-definition", "d", role="director").returncode == 0
    sb.write_file("r.md", "a\nb\nc\n")
    assert sb.run("report", "submit", "--dispatch", "d-1-1", "--src",
                  str(sb.root / "r.md"), role="unit").returncode == 0
    assert sb.run("claim", "reject", "--dispatch", "d-1-1", "--text", "x",
                  "--reason", "first", role="verifier").returncode == 0
    rec = sb.root / "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    assert rec.exists()

    # (1) hand-modify an existing record -> reject (write-once), even under verifier
    rec.write_text("# tampered verdict\n")
    assert sb.git("add", str(rec)).returncode == 0
    r = sb.git("commit", "-m", "tamper adj",
               env_extra={"HT_COMMIT": "1", "HT_ROLE": "verifier"})
    assert r.returncode != 0
    assert "write-once" in r.stderr and "B4 §5" in r.stderr
    assert sb.git("reset", "--hard", "HEAD").returncode == 0  # discard staged tamper

    # (2) wrong-role addition of a new record -> reject (verifier stamp)
    forged = sb.root / "trees/L4/nodes/1/adjudications/d-1-1-a2.md"
    forged.write_text("# forged verdict\n")
    assert sb.git("add", str(forged)).returncode == 0
    r = sb.git("commit", "-m", "forge adj",
               env_extra={"HT_COMMIT": "1", "HT_ROLE": "director"})
    assert r.returncode != 0
    assert "verifier-authored" in r.stderr and "B4 §5" in r.stderr
    sb.git("reset", "--hard", "HEAD")
    forged.unlink(missing_ok=True)  # remove the untracked forgery

    # (3) verifier addition of -a2 via the tool -> passes
    r = sb.run("claim", "reject", "--dispatch", "d-1-1", "--text", "y",
               "--reason", "second", role="verifier")
    assert r.returncode == 0, r.stderr
    assert (sb.root / "trees/L4/nodes/1/adjudications/d-1-1-a2.md").exists()
