from __future__ import annotations

import copy
import json

import pytest

from harnessd import addressing, fencing, ledger, watchdog
from harnessd.spawn import chokepoint


PARENT = "proj#exec"
C1 = "proj/a#exec"
C2 = "proj/b#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    return tmp_path


def _binding(address, *, parent=None, state="running", extra=None):
    binding = {
        "node_address": address,
        "parent_address": parent,
        "state": state,
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": fencing.mint_owner_token(address, "sub", "session", 1),
        "level": "L3",
        "last_applied_seq": 0,
        "last_inbox_acked_offset": 0,
    }
    binding.update(extra or {})
    return binding


def _seed(*bindings):
    ledger.write_binding(
        {binding["node_address"]: copy.deepcopy(binding) for binding in bindings},
        _lock_held=True,
    )


def _rows(root, address=PARENT):
    path = addressing.inbox_path(address, root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_product_children_append_silently_until_last_completion(runtime):
    parent = _binding(PARENT)
    first = _binding(C1, parent=PARENT, state="done")
    second = _binding(C2, parent=PARENT)
    _seed(parent, first, second)

    chokepoint._notify_parent_of_collapse(C1, first, "DONE")
    assert [row["type"] for row in _rows(runtime)] == ["child_collapsed"]
    assert watchdog.inbox_has_unacked(parent, parent) is False

    bindings = ledger.all_nodes()
    bindings[C2]["state"] = "failed"
    bindings[C2]["generation"] = 2
    ledger.write_binding(bindings, _lock_held=True)
    chokepoint._notify_parent_of_collapse(C2, bindings[C2], "FAILED")

    rows = _rows(runtime)
    assert [row["type"] for row in rows] == [
        "child_collapsed",
        "child_collapsed",
        "barrier_complete",
    ]
    assert rows[-1]["cohort"] == "product"
    assert watchdog.inbox_has_unacked(parent, parent) is True


def test_recovery_is_idempotent_and_new_nonterminal_child_rearms_barrier(runtime):
    parent = _binding(PARENT)
    first = _binding(C1, parent=PARENT, state="done")
    _seed(parent, first)
    chokepoint._notify_parent_of_collapse(C1, first, "DONE")
    chokepoint._notify_parent_of_collapse(C1, first, "DONE")
    assert len([row for row in _rows(runtime) if row["type"] == "barrier_complete"]) == 1

    new_child = _binding(C2, parent=PARENT, state="running")
    bindings = ledger.all_nodes()
    bindings[C2] = new_child
    ledger.write_binding(bindings, _lock_held=True)
    bindings[C2]["state"] = "done"
    bindings[C2]["generation"] = 2
    ledger.write_binding(bindings, _lock_held=True)
    chokepoint._notify_parent_of_collapse(C2, bindings[C2], "DONE")
    barriers = [row for row in _rows(runtime) if row["type"] == "barrier_complete"]
    assert len(barriers) == 2
    assert barriers[0]["barrier_id"] != barriers[1]["barrier_id"]


def test_review_check_cohort_is_independent_from_product(runtime):
    producer = _binding(
        C1,
        parent=PARENT,
        extra={
            "gate_state": "candidate_submitted",
            "gate_review_address": "proj/a#review",
            "gate_id": "gate-1",
        },
    )
    review = _binding(
        "proj/a#review",
        parent=PARENT,
        extra={"gate_for": C1},
    )
    check1 = _binding(
        "proj/a/reviews/gate-1/reviewers/one#exec",
        parent="proj/a#review",
        state="done",
        extra={
            "review_check_for": "proj/a#review",
            "review_check_candidate": C1,
            "gate_id": "gate-1",
            "role_variant": "L4+#review-check",
        },
    )
    check2 = _binding(
        "proj/a/reviews/gate-1/reviewers/two#exec",
        parent="proj/a#review",
        state="done",
        extra={
            "review_check_for": "proj/a#review",
            "review_check_candidate": C1,
            "gate_id": "gate-1",
            "role_variant": "L4+#review-check",
        },
    )
    _seed(_binding(PARENT), producer, review, check1, check2)

    chokepoint._notify_parent_of_collapse(check2["node_address"], check2, "DONE")

    rows = _rows(runtime, "proj/a#review")
    assert rows[-1]["type"] == "barrier_complete"
    assert rows[-1]["cohort"] == "review_check"
    assert not _rows(runtime, PARENT)


def test_unknown_and_legacy_types_default_wake(runtime):
    parent = _binding(PARENT)
    _seed(parent)
    inbox = addressing.inbox_path(PARENT, runtime)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps({"type": "future-extension"}) + "\n", encoding="utf-8")
    assert watchdog.inbox_has_unacked(parent, parent) is True

    inbox.write_text(
        json.dumps({"type": "child_collapsed", "child": C1}) + "\n",
        encoding="utf-8",
    )
    assert watchdog.inbox_has_unacked(parent, parent) is True
