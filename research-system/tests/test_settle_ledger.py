"""Settlement resolutions (revive/demote) and the settlement -> ledger trigger/write
split (A1 §1/§7) — the verifier executes the queued ledger write and fills demoted_to."""

from __future__ import annotations

from conftest import Sandbox, seed_worked_node


def _park(sb: Sandbox, node: str = "1") -> None:
    assert sb.run("node", "park", "--node", node, "--rationale", "swap", role="director").returncode == 0


def test_settle_revived_reopens_and_reforks(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")
    _park(sb, "1")
    assert sb.run("settle", "--node", "1", "--resolution", "revived",
                  "--rationale", "back in", role="director").returncode == 0
    node = sb.load("trees/L4/nodes/1/node.json")
    assert node["status"] == "worked"
    assert node["conflicts"][-1]["settlement"] == "revived"
    # a new fork entry stamped at the current epoch (A1 §2.1)
    assert node["git_binding"]["fork"][-1]["reason"] == "revived"


def test_settle_demoted_then_ledger_from_settlement(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")  # node 1 has a granted claim
    _park(sb, "1")
    assert sb.run("settle", "--node", "1", "--resolution", "demoted",
                  "--rationale", "weaker generalization", role="director").returncode == 0
    node = sb.load("trees/L4/nodes/1/node.json")
    assert node["status"] == "closed"
    conflict = node["conflicts"][-1]
    assert conflict["settlement"] == "demoted" and conflict["demoted_to"] is None

    # director may NOT execute the ledger write (research section = verifier)
    bad = sb.run("ledger", "create", "--section", "research", "--text", "gen",
                 "--from-settlement", "node#1", "--component", "L4", role="director")
    assert bad.returncode != 0 and "A1 §10" in bad.stderr

    # verifier executes it: creates the research entry AND fills demoted_to
    ok = sb.run("ledger", "create", "--section", "research", "--text", "generalization",
                "--from-settlement", "node#1", "--component", "L4", "--book", "L4",
                role="verifier", env_extra={"HT_LANE": "L4"})
    assert ok.returncode == 0, ok.stderr
    entry = sb.load("ledger/L4/research/L-1.json")
    assert entry["proposed_by"] == "settlement#node-1"
    # support seeded from the demoted branch's granted claims (one echo each)
    assert entry["support_count"] == 1 and entry["echoes"][0]["source_ref"] == "claim#c-1-1"
    node = sb.load("trees/L4/nodes/1/node.json")
    assert node["conflicts"][-1]["demoted_to"] == "ledger#L-1"


def test_ledger_status_transition_director(tree: Sandbox):
    sb = tree
    assert sb.run("ledger", "create", "--section", "user", "--text", "idea",
                  role="user").returncode == 0
    r = sb.run("ledger", "status", "--entry", "L-1", "--to", "minted",
               "--ref", "node#1", role="director")
    assert r.returncode == 0, r.stderr
    assert sb.load("ledger/top/user/L-1.json")["status"] == {
        "state": "minted", "ref": "node#1", "reason": None
    }
