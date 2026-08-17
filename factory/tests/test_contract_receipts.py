from __future__ import annotations

from harnessd import addressing, contracts, ledger, notary, turn_state


def test_contract_receipt_map_is_keyed_by_physical_home_and_package_is_aggregate(tmp_path):
    home = tmp_path / "l4" / "contracts" / "accepted-tests" / "parser"
    home.mkdir(parents=True)
    first = home / "test_one.py"
    second = home / "test_two.py"
    first.write_text("def test_one(): pass\n", encoding="utf-8")
    second.write_text("def test_two(): pass\n", encoding="utf-8")
    stamped = notary.stamp(home, members=[second, first], root_label="tests")

    receipt = contracts.contract_receipt(
        "proj/widget/parser#exec",
        "proj/widget#exec",
        home,
        stamped,
    )
    receipts = contracts.merge_receipts(receipt)

    assert list(receipts) == [str(home.resolve())]
    assert receipts[str(home.resolve())]["fingerprint"] == stamped["sha256"]
    assert receipts[str(home.resolve())]["stamp"]["file_count"] == 2
    assert not any("test_one.py" in key for key in receipts)


def test_stale_holder_query_is_a_pure_ledger_join_and_excludes_terminal_by_default(tmp_path):
    artifact = tmp_path / "brief.md"
    artifact.write_text("v1\n", encoding="utf-8")
    v1 = notary.stamp(artifact)
    artifact.write_text("v2\n", encoding="utf-8")
    v2 = notary.stamp(artifact)
    owner_address = "proj#exec"
    holder_address = "proj/child#exec"
    receipt = contracts.contract_receipt(holder_address, owner_address, artifact, v1)
    version = contracts.version_entry(
        owner_address=owner_address,
        artifact=artifact,
        stamped=v2,
        revision_record_ref=tmp_path / "revision.json",
        lineage=[],
    )
    bindings = {
        owner_address: {
            "node_address": owner_address,
            "state": "running",
            "contract_versions": {str(artifact.resolve()): version},
        },
        holder_address: {
            "node_address": holder_address,
            "state": "running",
            "contract_receipts": {str(artifact.resolve()): receipt},
        },
    }
    before = repr(bindings)

    stale = contracts.stale_receipt_holders(bindings)

    assert len(stale) == 1
    assert stale[0]["holder_address"] == holder_address
    assert repr(bindings) == before
    bindings[holder_address]["state"] = "done"
    assert contracts.stale_receipt_holders(bindings) == []
    assert len(contracts.stale_receipt_holders(bindings, include_terminal=True)) == 1


def test_stale_receipt_does_not_enter_checklist_waits_or_turn_end_contract(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    owner_address = "proj#exec"
    holder_address = "proj/child#exec"
    node_dir = addressing.node_dir(holder_address, tmp_path)
    node_dir.mkdir(parents=True)
    artifact = node_dir / "brief.md"
    artifact.write_text("v1\n", encoding="utf-8")
    v1 = notary.stamp(artifact)
    artifact.write_text("v2\n", encoding="utf-8")
    v2 = notary.stamp(artifact)
    receipt = contracts.contract_receipt(holder_address, owner_address, artifact, v1)
    owner = {
        "node_address": owner_address,
        "parent_address": None,
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": "owner-token",
        "contract_versions": {
            str(artifact.resolve()): contracts.version_entry(
                owner_address=owner_address,
                artifact=artifact,
                stamped=v2,
                revision_record_ref=tmp_path / "revision.json",
            )
        },
    }
    holder = {
        "node_address": holder_address,
        "parent_address": owner_address,
        "level": "L5",
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": "holder-token",
        "workspace": str(node_dir),
        "contract_receipts": {str(artifact.resolve()): receipt},
    }
    ledger.write_binding(
        {owner_address: owner, holder_address: holder},
        _lock_held=True,
    )

    checklist = turn_state.build_checklist(
        holder_address,
        holder,
        runtime_root=tmp_path,
    )
    waits = turn_state.ledger_wait_reasons(
        holder_address,
        holder,
        runtime_root=tmp_path,
    )

    assert all("contract" not in item["item_id"] for item in checklist["items"])
    assert waits == ()
