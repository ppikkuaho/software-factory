"""Work item 2: issue lifecycle, joins, closure, and LCA ledger docking."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import Sandbox
from ht import schemas
from ht.errors import HtError


def _mint(
    sandbox: Sandbox,
    *,
    title: str = "Trace the cross-level fault",
    lanes: tuple[str, ...] = ("L4",),
    provenance: tuple[str, ...] = ("user-seed#U-1",),
    role: str = "pc",
):
    return sandbox.run(
        "issue",
        "mint",
        "--title",
        title,
        "--question",
        "Which attribution survives?",
        "--done-definition",
        "A lane hypothesis is supported or every attribution is escalated.",
        "--provenance",
        *provenance,
        "--lanes",
        *lanes,
        role=role,
    )


def _tree(sandbox: Sandbox, lane: str) -> None:
    result = sandbox.run(
        "tree",
        "init",
        lane,
        "--root-question",
        f"Question for {lane}?",
        role="director",
    )
    assert result.returncode == 0, result.stderr


def _activate(sandbox: Sandbox, issue_id: str = "I-1") -> None:
    ratified = sandbox.run(
        "issue", "ratify", "--issue", issue_id, role="user"
    )
    assert ratified.returncode == 0, ratified.stderr
    activated = sandbox.run(
        "issue", "activate", "--issue", issue_id, role="user"
    )
    assert activated.returncode == 0, activated.stderr


def _node(sandbox: Sandbox, lane: str = "L4", from_issue: str | None = None) -> None:
    args = [
        "node",
        "mint",
        "--tree",
        lane,
        "--root",
        "--premise",
        "Test the producing-side attribution.",
        "--rationale",
        "Cheapest discriminating test.",
    ]
    if from_issue is not None:
        args.extend(["--from-issue", from_issue])
    result = sandbox.run(*args, role="director")
    assert result.returncode == 0, result.stderr


def _dispatch(
    sandbox: Sandbox,
    *,
    issue: str | None = "I-1",
    node: str = "1",
    lane: str = "L4",
):
    args = [
        "dispatch",
        "create",
        "--tree",
        lane,
        "--node",
        node,
        "--question",
        "Run the discriminating test.",
        "--done-definition",
        "The result is reproducible.",
    ]
    if issue is not None:
        args.extend(["--issue-ref", issue])
    return sandbox.run(*args, role="director")


def test_issue_mint_computes_min_numbered_lca_and_initializes_minimal_state(
    sandbox: Sandbox,
) -> None:
    result = _mint(sandbox, lanes=("L4+", "L2", "L4"))

    assert result.returncode == 0, result.stderr
    issue = sandbox.load("tier1/issues/I-1.json")
    assert issue == {
        "id": "I-1",
        "title": "Trace the cross-level fault",
        "provenance": ["user-seed#U-1"],
        "scope": "L2",
        "question": "Which attribution survives?",
        "done_definition": (
            "A lane hypothesis is supported or every attribution is escalated."
        ),
        "lanes": ["L4+", "L2", "L4"],
        "subgoals": [],
        "observatory_attachments": [],
        "status": "proposed",
        "closure": None,
    }


def test_issue_mint_rejects_missing_source_category_and_non_level_lane(
    sandbox: Sandbox,
) -> None:
    bad_source = _mint(sandbox, provenance=("report#R-1",))
    assert bad_source.returncode != 0
    assert "ledger-entry, observatory, or user-seed" in bad_source.stderr

    bad_lane = _mint(sandbox, lanes=("methodology",))
    assert bad_lane.returncode != 0
    assert "non-L1-L5 lanes" in bad_lane.stderr
    assert "scope was not guessed" in bad_lane.stderr


@pytest.mark.parametrize(
    ("command", "role", "extra"),
    [
        ("mint", "director", ()),
        ("ratify", "pc", ()),
        ("activate", "pc", ()),
        ("park", "director", ()),
        ("withdraw", "verifier", ()),
        ("close", "unit", ("--text", "Closure paragraph.")),
        ("subgoal-add", "harness", ("--ref", "dispatch#d-1-1")),
    ],
)
def test_issue_command_role_rejection_matrix_names_authority(
    sandbox: Sandbox,
    command: str,
    role: str,
    extra: tuple[str, ...],
) -> None:
    if command == "mint":
        result = _mint(sandbox, role=role)
    else:
        assert _mint(sandbox).returncode == 0
        if command == "activate":
            assert sandbox.run(
                "issue", "ratify", "--issue", "I-1", role="user"
            ).returncode == 0
        elif command in {"park", "withdraw", "close"}:
            _activate(sandbox)
        elif command == "subgoal-add":
            assert sandbox.run(
                "tree", "init", "L4", "--root-question", "Synthetic tree",
                role="director",
            ).returncode == 0
            _node(sandbox, from_issue="I-1")
            assert _dispatch(sandbox, issue="I-1").returncode == 0
        result = sandbox.run(
            "issue", command, "--issue", "I-1", *extra, role=role
        )

    assert result.returncode != 0
    assert "A1 §10 write-authority" in result.stderr


def test_phase_authority_routes_ratify_and_activate_without_duplication(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox).returncode == 0
    rejected = sandbox.run("issue", "ratify", "--issue", "I-1", role="pc")
    assert rejected.returncode != 0
    assert "owner: user" in rejected.stderr

    ratified = sandbox.run("issue", "ratify", "--issue", "I-1", role="user")
    assert ratified.returncode == 0, ratified.stderr
    activated = sandbox.run("issue", "activate", "--issue", "I-1", role="user")
    assert activated.returncode == 0, activated.stderr

    assert sandbox.run("phase", "set", "autonomy", role="user").returncode == 0
    assert _mint(sandbox, title="Second issue").returncode == 0
    assert sandbox.run(
        "issue", "ratify", "--issue", "I-2", role="pc"
    ).returncode == 0
    assert sandbox.run(
        "issue", "activate", "--issue", "I-2", role="pc"
    ).returncode == 0


def test_pc_terminal_writes_and_subgoal_append_succeed(sandbox: Sandbox) -> None:
    assert _mint(sandbox).returncode == 0
    assert _mint(sandbox, title="Withdraw this issue").returncode == 0
    assert _mint(sandbox, title="Park then withdraw this issue").returncode == 0
    _tree(sandbox, "L4")
    _node(sandbox, from_issue="I-1")
    assert _dispatch(sandbox, issue="I-1").returncode == 0

    added = sandbox.run(
        "issue",
        "subgoal-add",
        "--issue",
        "I-1",
        "--ref",
        "dispatch#d-1-1",
        role="pc",
    )
    assert added.returncode == 0, added.stderr
    _activate(sandbox, "I-1")
    parked = sandbox.run("issue", "park", "--issue", "I-1", role="pc")
    withdrawn = sandbox.run("issue", "withdraw", "--issue", "I-2", role="pc")
    _activate(sandbox, "I-3")
    assert sandbox.run(
        "issue", "park", "--issue", "I-3", role="pc"
    ).returncode == 0
    parked_withdrawn = sandbox.run(
        "issue", "withdraw", "--issue", "I-3", role="pc"
    )
    assert parked.returncode == 0, parked.stderr
    assert withdrawn.returncode == 0, withdrawn.stderr
    assert parked_withdrawn.returncode == 0, parked_withdrawn.stderr
    assert sandbox.load("tier1/issues/I-1.json")["status"] == "parked"
    assert sandbox.load("tier1/issues/I-1.json")["subgoals"] == [
        "tree#L4/dispatch#d-1-1"
    ]
    assert sandbox.load("tier1/issues/I-2.json")["status"] == "withdrawn"
    assert sandbox.load("tier1/issues/I-3.json")["status"] == "withdrawn"


def test_issue_commands_enforce_lifecycle_order_and_activate_help_policy(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox).returncode == 0
    early = sandbox.run("issue", "activate", "--issue", "I-1", role="user")
    assert early.returncode != 0
    assert "requires parked|ratified" in early.stderr

    _activate(sandbox)
    backwards = sandbox.run("issue", "ratify", "--issue", "I-1", role="user")
    assert backwards.returncode != 0
    assert "requires proposed" in backwards.stderr

    help_result = sandbox.run("issue", "activate", "--help")
    assert help_result.returncode == 0
    normalized_help = " ".join(help_result.stdout.split())
    assert "evidence for a machinery upgrade" in normalized_help
    assert "never change this policy silently" in normalized_help


def test_parked_reactivation_is_phase_gated_and_allowed_for_authorized_role(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox).returncode == 0
    _activate(sandbox)
    assert sandbox.run(
        "issue", "park", "--issue", "I-1", role="pc"
    ).returncode == 0

    pc_rejected = sandbox.run(
        "issue", "activate", "--issue", "I-1", role="pc"
    )
    assert pc_rejected.returncode != 0
    assert "owner: user" in pc_rejected.stderr
    assert sandbox.load("tier1/issues/I-1.json")["status"] == "parked"

    user_reactivated = sandbox.run(
        "issue", "activate", "--issue", "I-1", role="user"
    )
    assert user_reactivated.returncode == 0, user_reactivated.stderr
    assert sandbox.run(
        "issue", "park", "--issue", "I-1", role="pc"
    ).returncode == 0
    assert sandbox.run(
        "phase", "set", "autonomy", role="user"
    ).returncode == 0
    pc_reactivated = sandbox.run(
        "issue", "activate", "--issue", "I-1", role="pc"
    )
    assert pc_reactivated.returncode == 0, pc_reactivated.stderr
    assert sandbox.load("tier1/issues/I-1.json")["status"] == "active"


def test_capacity_one_warning_names_blocking_issue_and_does_not_block(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox, title="Blocking cascade").returncode == 0
    assert _mint(sandbox, title="Second cascade").returncode == 0
    for issue_id in ("I-1", "I-2"):
        assert sandbox.run(
            "issue", "ratify", "--issue", issue_id, role="user"
        ).returncode == 0

    first = sandbox.run("issue", "activate", "--issue", "I-1", role="user")
    assert first.returncode == 0, first.stderr
    assert "CAPACITY-1" not in first.stderr

    second = sandbox.run("issue", "activate", "--issue", "I-2", role="user")
    assert second.returncode == 0, second.stderr
    assert "CAPACITY-1 WARNING" in second.stderr
    assert "I-1" in second.stderr
    assert "Blocking cascade" in second.stderr
    assert sandbox.load("tier1/issues/I-2.json")["status"] == "active"


def test_concurrent_issue_mints_keep_ids_unique_under_global_mutex(
    sandbox: Sandbox,
) -> None:
    barrier = threading.Barrier(2)

    def create(title: str):
        barrier.wait()
        last = None
        for _ in range(20):
            last = _mint(sandbox, title=title)
            if last.returncode == 0:
                return last
            if "global merge/ledger mutex is contended" not in last.stderr:
                return last
            time.sleep(0.01)
        return last

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("Concurrent A", "Concurrent B")))

    assert all(result is not None and result.returncode == 0 for result in results)
    assert sorted(path.name for path in (sandbox.root / "tier1/issues").glob("*.json")) == [
        "I-1.json",
        "I-2.json",
    ]


def test_node_and_dispatch_write_only_their_join_side_and_hygiene_warns(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox).returncode == 0
    _tree(sandbox, "L4")
    _node(sandbox, from_issue="I-1")
    dispatch = _dispatch(sandbox)
    assert dispatch.returncode == 0, dispatch.stderr

    assert sandbox.load("trees/L4/nodes/1/node.json")["minted_from"] == "issue#I-1"
    assert sandbox.load(
        "trees/L4/nodes/1/dispatches/d-1-1.json"
    )["issue_ref"] == "I-1"
    assert sandbox.load("tier1/issues/I-1.json")["subgoals"] == []

    warning = sandbox.run("validate", "--all")
    assert warning.returncode == 0, warning.stderr
    assert "WARNING: join asymmetry" in warning.stderr
    assert "dispatch d-1-1" in warning.stderr

    for ref in ("node#1", "dispatch#d-1-1"):
        added = sandbox.run(
            "issue", "subgoal-add", "--issue", "I-1", "--ref", ref, role="pc"
        )
        assert added.returncode == 0, added.stderr
    clean = sandbox.run("validate", "--all")
    assert clean.returncode == 0, clean.stderr
    assert "join asymmetry" not in clean.stderr


def test_issue_subgoal_without_backref_rejects_nonmutating(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox).returncode == 0
    _tree(sandbox, "L4")
    _node(sandbox)
    before = sandbox.load("tier1/issues/I-1.json")
    rejected = sandbox.run(
        "issue", "subgoal-add", "--issue", "I-1", "--ref", "node#1", role="pc"
    )
    assert rejected.returncode != 0
    assert "unaffiliated" in rejected.stderr
    assert sandbox.load("tier1/issues/I-1.json") == before


def test_join_schemas_reject_malformed_issue_refs(sandbox: Sandbox) -> None:
    assert _mint(sandbox).returncode == 0
    _tree(sandbox, "L4")
    _node(sandbox, from_issue="I-1")
    assert _dispatch(sandbox).returncode == 0
    node = sandbox.load("trees/L4/nodes/1/node.json")
    dispatch = sandbox.load("trees/L4/nodes/1/dispatches/d-1-1.json")

    node["minted_from"] = "issue#broken"
    dispatch["issue_ref"] = "issue#I-1"
    with pytest.raises(HtError, match="schema-nonconforming node"):
        schemas.validate(sandbox.root / "system/schemas", "node", node)
    with pytest.raises(HtError, match="schema-nonconforming dispatch"):
        schemas.validate(sandbox.root / "system/schemas", "dispatch", dispatch)


@pytest.mark.parametrize("outcome", [None, "blocked", "retry-operational"])
def test_close_blocks_each_nonterminal_dispatch_by_issue_ref(
    sandbox: Sandbox,
    outcome: str | None,
) -> None:
    assert _mint(sandbox).returncode == 0
    _activate(sandbox)
    _tree(sandbox, "L4")
    _node(sandbox)
    assert _dispatch(sandbox).returncode == 0
    if outcome is not None:
        assert sandbox.run(
            "dispatch",
            "outcome",
            "--dispatch",
            "d-1-1",
            "--outcome",
            outcome,
            role="harness",
        ).returncode == 0

    result = sandbox.run(
        "issue", "close", "--issue", "I-1", "--text", "Cannot close yet.", role="pc"
    )
    assert result.returncode != 0
    assert "d-1-1" in result.stderr
    assert (outcome or "pending") in result.stderr
    assert sandbox.load("tier1/issues/I-1.json")["closure"] is None


def test_close_all_terminal_records_dispositions_atomically(sandbox: Sandbox) -> None:
    assert _mint(sandbox).returncode == 0
    _activate(sandbox)
    _tree(sandbox, "L4")
    _node(sandbox)
    assert _dispatch(sandbox).returncode == 0
    assert _dispatch(sandbox).returncode == 0
    for dispatch_id, outcome in (("d-1-1", "completed"), ("d-1-2", "recalled")):
        result = sandbox.run(
            "dispatch",
            "outcome",
            "--dispatch",
            dispatch_id,
            "--outcome",
            outcome,
            role="harness",
        )
        assert result.returncode == 0, result.stderr

    closed = sandbox.run(
        "issue",
        "close",
        "--issue",
        "I-1",
        "--text",
        "The attribution chase is settled.",
        "--ref",
        "issue#I-1",
        role="pc",
    )
    assert closed.returncode == 0, closed.stderr
    issue = sandbox.load("tier1/issues/I-1.json")
    assert issue["status"] == "closed"
    assert issue["closure"] == {
        "text": "The attribution chase is settled.",
        "refs": ["issue#I-1"],
        "dispositions": [
            {"ref": "tree#L4/dispatch#d-1-1", "status": "completed"},
            {"ref": "tree#L4/dispatch#d-1-2", "status": "recalled"},
        ],
    }

    for command, extra, role in (
        ("ratify", (), "user"),
        ("activate", (), "user"),
        ("park", (), "pc"),
        ("withdraw", (), "pc"),
        ("subgoal-add", ("--ref", "node#2"), "pc"),
        ("close", ("--text", "Rewrite closure."), "pc"),
    ):
        rejected = sandbox.run(
            "issue", command, "--issue", "I-1", *extra, role=role
        )
        assert rejected.returncode != 0
        assert "closed and immutable" in rejected.stderr
    assert sandbox.load("tier1/issues/I-1.json") == issue


def test_pending_dispatch_on_demoted_node_is_terminal_for_issue_close(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox).returncode == 0
    _activate(sandbox)
    _tree(sandbox, "L4")
    _node(sandbox)
    assert _dispatch(sandbox).returncode == 0

    sandbox.write_file("src.md", "# Report\nline two\nline three\nline four\n")
    report = sandbox.run(
        "report",
        "submit",
        "--dispatch",
        "d-1-1",
        "--src",
        str(sandbox.root / "src.md"),
        role="unit",
    )
    assert report.returncode == 0, report.stderr
    anchor = "trees/L4/nodes/1/reports/d-1-1-report.md:1:3"
    granted = sandbox.run(
        "claim",
        "grant",
        "--dispatch",
        "d-1-1",
        "--text",
        "The attribution is weakened by the test.",
        "--proposed-tier",
        "2",
        "--granted-tier",
        "2",
        "--standing-class",
        "trunk",
        "--anchor",
        anchor,
        role="verifier",
    )
    assert granted.returncode == 0, granted.stderr
    assert sandbox.run(
        "node",
        "park",
        "--node",
        "1",
        "--rationale",
        "Demote this branch.",
        role="director",
    ).returncode == 0
    settled = sandbox.run(
        "settle",
        "--node",
        "1",
        "--resolution",
        "demoted",
        role="director",
    )
    assert settled.returncode == 0, settled.stderr
    node = sandbox.load("trees/L4/nodes/1/node.json")
    assert node["status"] == "closed"
    assert node["conflicts"][-1]["settlement"] == "demoted"

    closed = sandbox.run(
        "issue",
        "close",
        "--issue",
        "I-1",
        "--text",
        "The node demotion disposes the pending sub-dispatch.",
        role="pc",
    )
    assert closed.returncode == 0, closed.stderr
    assert sandbox.load("tier1/issues/I-1.json")["closure"] == {
        "text": "The node demotion disposes the pending sub-dispatch.",
        "refs": [],
        "dispositions": [
            {"ref": "tree#L4/dispatch#d-1-1", "status": "demoted"}
        ],
    }


def test_close_scan_ignores_subgoal_bookkeeping_and_unrelated_dispatches(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox).returncode == 0
    _activate(sandbox)
    _tree(sandbox, "L4")
    _node(sandbox)
    assert _dispatch(sandbox, issue=None).returncode == 0
    rejected = sandbox.run(
        "issue",
        "subgoal-add",
        "--issue",
        "I-1",
        "--ref",
        "dispatch#d-1-1",
        role="pc",
    )
    assert rejected.returncode != 0
    assert "unaffiliated" in rejected.stderr

    closed = sandbox.run(
        "issue", "close", "--issue", "I-1", "--text", "No enrolled work.", role="pc"
    )
    assert closed.returncode == 0, closed.stderr
    assert sandbox.load("tier1/issues/I-1.json")["closure"]["refs"] == []
    assert sandbox.load("tier1/issues/I-1.json")["closure"]["dispositions"] == []


def test_cross_lane_dispatch_id_collisions_are_qualified_in_closure(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox, lanes=("L2", "L4")).returncode == 0
    _activate(sandbox)
    for lane in ("L2", "L4"):
        _tree(sandbox, lane)
        _node(sandbox, lane=lane)
        created = _dispatch(sandbox, lane=lane)
        assert created.returncode == 0, created.stderr
        outcome = sandbox.run(
            "dispatch",
            "outcome",
            "--tree",
            lane,
            "--dispatch",
            "d-1-1",
            "--outcome",
            "completed",
            role="harness",
        )
        assert outcome.returncode == 0, outcome.stderr

    closed = sandbox.run(
        "issue", "close", "--issue", "I-1", "--text", "Both lanes settled.", role="pc"
    )
    assert closed.returncode == 0, closed.stderr
    assert sandbox.load("tier1/issues/I-1.json")["closure"]["refs"] == []
    assert sandbox.load("tier1/issues/I-1.json")["closure"]["dispositions"] == [
        {"ref": "tree#L2/dispatch#d-1-1", "status": "completed"},
        {"ref": "tree#L4/dispatch#d-1-1", "status": "completed"},
    ]


def test_top_book_issue_provenance_requires_live_scope_lane(sandbox: Sandbox) -> None:
    assert _mint(sandbox, lanes=("L4", "L2")).returncode == 0
    _tree(sandbox, "L2")

    wrong = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        "top",
        "--text",
        "Cross-level attribution result.",
        "--provenance",
        "issue#I-1",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert wrong.returncode != 0
    assert "requires verifier lane 'L2', got 'L4'" in wrong.stderr

    accepted = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        "top",
        "--text",
        "Distinct cross-level scope finding.",
        "--provenance",
        "issue#I-1",
        role="verifier",
        env_extra={"HT_LANE": "L2"},
    )
    assert accepted.returncode == 0, accepted.stderr
    entry = sandbox.load("ledger/top/research/L-1.json")
    assert entry["lane"] == "L2"
    assert entry["intended_scope"] is None


def test_top_book_missing_scope_tree_falls_back_and_stamps_visible_debt(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox, lanes=("L4", "L2")).returncode == 0

    created = sandbox.run(
        "ledger",
        "create",
        "--section",
        "observatory",
        "--book",
        "top",
        "--text",
        "Fallback-scoped cross-level finding.",
        "--provenance",
        "issue#I-1",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert created.returncode == 0, created.stderr
    assert "LCA FALLBACK WARNING" in created.stderr
    assert "L2" in created.stderr
    entry = sandbox.load("ledger/top/observatory/L-1.json")
    assert entry["lane"] == "L4"
    assert entry["intended_scope"] == "L2"

    validation = sandbox.run("validate", "--all")
    assert validation.returncode == 0, validation.stderr
    assert "INVALID:" not in validation.stderr
    assert (
        "LCA re-homing debt: L-1 (book top) intended_scope=L2"
        in validation.stdout
    )
    assert "WARNING: LCA re-homing debt" not in validation.stderr


def test_top_book_scope_lane_without_tree_needs_no_fallback_debt(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox, lanes=("L4", "L2")).returncode == 0

    created = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        "top",
        "--text",
        "Scope-docked finding before lane tree initialization.",
        "--provenance",
        "issue#I-1",
        role="verifier",
        env_extra={"HT_LANE": "L2"},
    )
    assert created.returncode == 0, created.stderr
    assert "LCA FALLBACK WARNING" not in created.stderr
    entry = sandbox.load("ledger/top/research/L-1.json")
    assert entry["lane"] == "L2"
    assert entry["intended_scope"] is None
    validation = sandbox.run("validate", "--all")
    assert validation.returncode == 0, validation.stderr
    assert "LCA re-homing debt" not in validation.stdout


@pytest.mark.parametrize("book", ["top", "L4"])
def test_issue_provenance_must_exist_in_every_ledger_book(
    sandbox: Sandbox,
    book: str,
) -> None:
    _tree(sandbox, "L4")
    rejected = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        book,
        "--text",
        f"Missing issue provenance in {book}.",
        "--provenance",
        "issue#I-99",
        role="verifier",
        env_extra={"HT_LANE": "L4"},
    )
    assert rejected.returncode != 0
    assert "no such issue provenance ref 'issue#I-99'" in rejected.stderr


def test_unassigned_verifier_lane_rejection_never_prints_sentinel_repr(
    sandbox: Sandbox,
) -> None:
    assert _mint(sandbox, lanes=("L2",)).returncode == 0
    _tree(sandbox, "L2")
    rejected = sandbox.run(
        "ledger",
        "create",
        "--section",
        "research",
        "--book",
        "top",
        "--text",
        "Unassigned verifier cannot satisfy docking.",
        "--provenance",
        "issue#I-1",
        role="verifier",
    )
    assert rejected.returncode != 0
    assert "requires verifier lane 'L2', got 'None'" in rejected.stderr
    assert "object at 0x" not in rejected.stderr


def test_issue_schema_still_forbids_closed_without_closure(sandbox: Sandbox) -> None:
    doc = {
        "id": "I-1",
        "title": "Test",
        "provenance": ["user-seed#U-1"],
        "scope": "L4",
        "question": "Question?",
        "done_definition": "Done.",
        "lanes": ["L4"],
        "subgoals": [],
        "status": "closed",
        "closure": None,
    }
    with pytest.raises(HtError, match="schema-nonconforming issue"):
        schemas.validate(sandbox.root / "system/schemas", "issue", doc)


def test_optional_ledger_intended_scope_schema_is_nullable(sandbox: Sandbox) -> None:
    schema = json.loads(
        (sandbox.root / "system/schemas/ledger_entry.schema.json").read_text()
    )
    assert schema["properties"]["intended_scope"]["type"] == ["string", "null"]
    assert "fallback debt marker" in schema["properties"]["intended_scope"]["$comment"]
