from __future__ import annotations

import json

import pytest

from harnessd import addressing, contracts, daemon, fencing, ledger, notary

OWNER = "proj#exec"
HOLDER = "proj/child#exec"


def _binding(address, *, parent=None, state="running"):
    return {
        "node_address": address,
        "parent_address": parent,
        "state": state,
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": fencing.mint_owner_token(address, "sub", "session", 1),
        "level": "L2",
        "last_applied_seq": 0,
        "last_inbox_acked_offset": 0,
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    owner = _binding(OWNER)
    holder = _binding(HOLDER, parent=OWNER)
    addressing.node_dir(OWNER, tmp_path).mkdir(parents=True)
    addressing.node_dir(HOLDER, tmp_path).mkdir(parents=True)
    artifact = addressing.node_dir(HOLDER, tmp_path) / "brief.md"
    artifact.write_text("v1\n", encoding="utf-8")
    v1 = notary.stamp(artifact)
    artifact.write_text("v2\n", encoding="utf-8")
    v2 = notary.stamp(artifact)
    revision = addressing.contract_revision_dir(OWNER, tmp_path) / "revision-two.json"
    revision.parent.mkdir()
    revision.write_text("{}\n", encoding="utf-8")
    receipt = contracts.contract_receipt(HOLDER, OWNER, artifact, v1)
    owner["contract_versions"] = {
        str(artifact.resolve()): contracts.version_entry(
            owner_address=OWNER,
            artifact=artifact,
            stamped=v2,
            revision_record_ref=revision,
            lineage=[
                {
                    "revision_id": "revision-two",
                    "revision_record_ref": str(revision),
                    "prior_fingerprint": v1["sha256"],
                    "new_fingerprint": v2["sha256"],
                }
            ],
        )
    }
    holder["contract_receipts"] = {str(artifact.resolve()): receipt}
    ledger.write_binding({OWNER: owner, HOLDER: holder}, _lock_held=True)
    return tmp_path


def _rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_ripple_is_one_deterministic_ordinary_3a_message_per_stale_live_holder(runtime):
    daemon._deliver_contract_amendments_best_effort()
    assert contracts.deliver_amendment_ripple(runtime_root=runtime) == 0

    records = ledger.read_binding(OWNER)["messages"]
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["target"] == HOLDER
    assert record["needs_answer"] is False
    assert record["tags"] == [contracts.AMENDMENT_TAG]
    assert record["metadata"]["amendment_authorizes_proceeding"] is True
    body = (
        addressing.node_dir(OWNER, runtime) / record["artifact"]
    ).read_text(encoding="utf-8")
    assert "authorizes proceeding" in body
    assert "Ordinary messages only persuade" in body
    rows = _rows(addressing.inbox_path(HOLDER, runtime))
    assert len(rows) == 1
    assert rows[0]["type"] == "message"


def test_ripple_does_not_wake_terminal_historical_holder(runtime):
    bindings = ledger.all_nodes()
    bindings[HOLDER]["state"] = "done"
    ledger.write_binding(bindings, _lock_held=True)

    assert contracts.deliver_amendment_ripple(runtime_root=runtime) == 0
    assert not addressing.inbox_path(HOLDER, runtime).exists()
