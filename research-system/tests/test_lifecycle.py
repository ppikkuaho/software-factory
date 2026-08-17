"""The legal lifecycle: one toy tree walked entirely through ht, asserting the
index files and decision_log track each step (executor brief 0.E)."""

from __future__ import annotations

from conftest import Sandbox


def _statuses(sb: Sandbox, rel: str) -> dict[str, str]:
    idx = sb.load(rel)
    return {n["id"]: n["status"] for n in idx["nodes"]}


def test_full_lifecycle(tree: Sandbox):
    sb = tree

    # mint root + a child
    assert sb.run("node", "mint", "--tree", "L4", "--root",
                  "--premise", "Root premise about the thing", "--rationale", "seed",
                  role="director").returncode == 0
    assert sb.run("node", "mint", "--tree", "L4", "--parent", "1",
                  "--premise", "Child hypothesis under root", "--rationale", "explore",
                  role="director").returncode == 0
    assert _statuses(sb, "trees/L4/index.json") == {"1": "unexplored", "1.1": "unexplored"}

    # cursor move
    assert sb.run("cursor", "move", "--tree", "L4", "--to", "1.1",
                  "--rationale", "focus", role="director").returncode == 0
    assert sb.load("trees/L4/index.json")["cursor"] == [{"node": "1.1", "allocation": "a1"}]

    # dispatch create -> git_binding appears at first dispatch
    assert sb.run("dispatch", "create", "--node", "1.1", "--question", "Test child",
                  "--done-definition", "5 conditions", role="director").returncode == 0
    node = sb.load("trees/L4/nodes/1.1/node.json")
    assert node["git_binding"]["branch"].startswith("ht/1.1-")
    assert node["git_binding"]["fork"] == [{"epoch": 0, "reason": "original"}]

    # archive write (git-ignored) + report submit (freeze hash)
    arch = sb.write_file("arch.txt", "raw\nlog\nlines\n")
    assert sb.run("archive", "write", "--dispatch", "d-1.1-1", "--src", str(arch),
                  "--name", "t.txt", role="unit").returncode == 0
    assert (sb.root / "trees/L4/nodes/1.1/archive/t.txt").exists()  # file present on disk
    # archive is git-ignored: not tracked
    ls = sb.git("ls-files", "trees/L4/nodes/1.1/archive/")
    assert ls.stdout.strip() == ""
    disp = sb.load("trees/L4/nodes/1.1/dispatches/d-1.1-1.json")
    assert disp["archive_ref"] == "trees/L4/nodes/1.1/archive/t.txt"

    rep = sb.write_file("rep.md", "# Report\nfinding line\nthird line\nfourth\n")
    assert sb.run("report", "submit", "--dispatch", "d-1.1-1", "--src", str(rep),
                  role="unit").returncode == 0
    disp = sb.load("trees/L4/nodes/1.1/dispatches/d-1.1-1.json")
    assert disp["report_hash"] and len(disp["report_hash"]) == 64

    # dispatch outcome (passive metering)
    assert sb.run("dispatch", "outcome", "--dispatch", "d-1.1-1", "--outcome", "completed",
                  "--tokens", "1200", "--wall-clock", "42.0", role="harness").returncode == 0

    # claim grant flips unexplored -> worked
    anchor = "trees/L4/nodes/1.1/reports/d-1.1-1-report.md:2:3"
    assert sb.run("claim", "grant", "--dispatch", "d-1.1-1",
                  "--text", "Child holds under 5 conditions", "--proposed-tier", "2",
                  "--granted-tier", "2", "--standing-class", "trunk", "--anchor", anchor, role="verifier").returncode == 0
    assert _statuses(sb, "trees/L4/index.json")["1.1"] == "worked"
    node = sb.load("trees/L4/nodes/1.1/node.json")
    assert len(node["claims"]) == 1 and node["claims"][0]["status"] == "granted"
    disp = sb.load("trees/L4/nodes/1.1/dispatches/d-1.1-1.json")
    assert disp["adjudications"] == [
        "tree#L4/adjudication#d-1.1-1-a1"
    ]

    # standing set
    assert sb.run("standing", "set", "--node", "1.1", "--standing", "supported",
                  "--note", "c-1.1-1 grants tier-2 support", role="verifier").returncode == 0
    assert sb.load("trees/L4/index.json")["nodes"][1]["standing"] == "supported"

    # park -> settle(closed)
    assert sb.run("node", "park", "--node", "1.1", "--rationale", "swap away",
                  role="director").returncode == 0
    assert _statuses(sb, "trees/L4/index.json")["1.1"] == "parked"
    assert sb.load("trees/L4/index.json")["nodes"][1]["unsettled_conflict"] is True
    assert sb.run("settle", "--node", "1.1", "--resolution", "closed",
                  "--rationale", "done", role="director").returncode == 0
    assert _statuses(sb, "trees/L4/index.json")["1.1"] == "closed"

    # second node minted, worked, closed via node close
    assert sb.run("node", "mint", "--tree", "L4", "--root",
                  "--premise", "Second top-level premise", "--rationale", "branch",
                  role="director").returncode == 0
    assert sb.run("dispatch", "create", "--node", "2", "--question", "Probe 2",
                  "--done-definition", "done", role="director").returncode == 0
    r2 = sb.write_file("rep2.md", "# R2\nalpha\nbeta\ngamma\n")
    assert sb.run("report", "submit", "--dispatch", "d-2-1", "--src", str(r2),
                  role="unit").returncode == 0
    assert sb.run("claim", "grant", "--dispatch", "d-2-1", "--text", "node 2 finding",
                  "--proposed-tier", "1", "--granted-tier", "1",
                  "--standing-class", "trunk", "--anchor", "trees/L4/nodes/2/reports/d-2-1-report.md:1:2",
                  role="verifier").returncode == 0
    assert _statuses(sb, "trees/L4/index.json")["2"] == "worked"
    assert sb.run("node", "close", "--node", "2", "--reason", "deprioritized",
                  role="director").returncode == 0
    assert _statuses(sb, "trees/L4/index.json")["2"] == "closed"

    # decision_log accumulated every director move
    log = sb.load("trees/L4/tree.json")["decision_log"]
    moves = [e["move"] for e in log]
    for expected in ["mint", "move", "dispatch", "park", "settle", "close"]:
        assert expected in moves, f"missing decision_log move '{expected}': {moves}"

    # live view: closed 1.1 collapses to a rollup line; node 1 (unexplored) stays live
    live = sb.load("trees/L4/index.live.json")
    live_ids = {n["id"] for n in live["nodes"]}
    assert "1" in live_ids  # live ancestor/root
    rollup_1_1 = next(n for n in live["nodes"] if n["id"] == "1.1")
    assert rollup_1_1["rollup"]["closed"] == 1
    # dead node 2 (closed leaf) also renders as its own rollup line
    assert "2" in live_ids and next(n for n in live["nodes"] if n["id"] == "2")["rollup"]["closed"] == 1
