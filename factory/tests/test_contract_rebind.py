from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessd import addressing, contracts, fencing, ledger, notary

OWNER = "proj#exec"
HOLDER = "proj/child#exec"


def _binding(address: str, *, parent=None) -> dict:
    return {
        "node_address": address,
        "parent_address": parent,
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": fencing.mint_owner_token(address, "sub", "session", 1),
        "level": "L2" if parent else "L1",
        "last_applied_seq": 0,
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    owner = _binding(OWNER)
    holder = _binding(HOLDER, parent=OWNER)
    owner["workspace"] = str(addressing.node_dir(OWNER, tmp_path))
    holder["workspace"] = str(addressing.node_dir(HOLDER, tmp_path))
    Path(owner["workspace"]).mkdir(parents=True)
    Path(holder["workspace"]).mkdir(parents=True)
    ledger.write_binding({OWNER: owner, HOLDER: holder}, _lock_held=True)
    return tmp_path


def _advance(
    version: dict,
    *,
    owner: dict,
    artifact: Path,
    body: str,
    reason: str,
) -> dict:
    artifact.chmod(0o644)
    artifact.write_text(body, encoding="utf-8")
    new_stamp = notary.stamp(artifact)
    ref, _record = contracts.mint_revision_record(
        owner_address=OWNER,
        owner_workspace=owner["workspace"],
        contract_path=artifact,
        prior_fingerprint=version["fingerprint"],
        new_fingerprint=new_stamp["sha256"],
        reason=reason,
        channel="intent_revision",
        channel_evidence={"owner_token": owner["owner_token"]},
    )
    prior_receipt = contracts.contract_receipt(
        HOLDER,
        OWNER,
        artifact,
        version["stamp"],
        revision_record_ref=version.get("revision_record_ref"),
    )
    result = notary.restamp(
        artifact,
        prior_receipt=prior_receipt,
        revision_record_ref=ref,
    )
    return contracts.append_lineage(version, result)


def _seed_three_versions(runtime):
    owner = ledger.read_binding(OWNER)
    holder = ledger.read_binding(HOLDER)
    artifact = Path(holder["workspace"]) / "client-brief" / "intent-spec.md"
    artifact.parent.mkdir()
    artifact.write_text("v1\n", encoding="utf-8")
    v1_stamp = notary.stamp(artifact, read_only=True)
    receipt = contracts.contract_receipt(HOLDER, OWNER, artifact, v1_stamp)
    v1 = contracts.version_entry(
        owner_address=OWNER,
        artifact=artifact,
        stamped=v1_stamp,
    )
    v2 = _advance(v1, owner=owner, artifact=artifact, body="v2\n", reason="second")
    v3 = _advance(v2, owner=owner, artifact=artifact, body="v3\n", reason="third")
    owner["contract_versions"] = {str(artifact.resolve()): v3}
    holder["contract_receipts"] = {str(artifact.resolve()): receipt}
    ledger.write_binding({OWNER: owner, HOLDER: holder}, _lock_held=True)
    return artifact, v1, v3


def _marker(runtime, artifact, version, *, rebind_id="adopt-latest"):
    holder = ledger.read_binding(HOLDER)
    directory = addressing.contract_rebind_dir(HOLDER, runtime)
    directory.mkdir()
    path = directory / f"{rebind_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rebind_id": rebind_id,
                "holder": HOLDER,
                "owner_token": holder["owner_token"],
                "contract_path": str(artifact.resolve()),
                "revision_record_ref": version["revision_record_ref"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_rebind_can_skip_multiple_revisions_when_full_lineage_is_contiguous(runtime):
    artifact, _v1, v3 = _seed_three_versions(runtime)
    marker = _marker(runtime, artifact, v3)

    assert contracts.service_rebind_marker(marker, runtime_root=runtime) is True

    live = ledger.read_binding(HOLDER)
    receipt = live["contract_receipts"][str(artifact.resolve())]
    assert receipt["fingerprint"] == v3["fingerprint"]
    assert receipt["revision_record_ref"] == v3["revision_record_ref"]
    assert marker.with_name(marker.name + ".done").exists()


def test_rebind_rejects_multi_revision_skip_when_lineage_has_a_gap(runtime):
    artifact, v1, v3 = _seed_three_versions(runtime)
    owner = ledger.read_binding(OWNER)
    broken = dict(v3)
    broken["lineage"] = [v3["lineage"][-1]]
    owner["contract_versions"][str(artifact.resolve())] = broken
    bindings = ledger.all_nodes()
    bindings[OWNER] = owner
    ledger.write_binding(bindings, _lock_held=True)
    marker = _marker(runtime, artifact, broken, rebind_id="broken-lineage")

    assert contracts.service_rebind_marker(marker, runtime_root=runtime) is False

    reason_path = marker.with_name(marker.name + ".rejected.reason")
    assert reason_path.exists()
    reason = reason_path.read_text(encoding="utf-8")
    assert "lineage gap after fingerprint" in reason
    assert v1["fingerprint"] in reason
    receipt = ledger.read_binding(HOLDER)["contract_receipts"][str(artifact.resolve())]
    assert receipt["fingerprint"] == v1["fingerprint"]


def test_rebind_rejects_stale_holder_fence(runtime):
    artifact, _v1, v3 = _seed_three_versions(runtime)
    marker = _marker(runtime, artifact, v3, rebind_id="stale-fence")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["owner_token"] = "stale-token"
    marker.write_text(json.dumps(payload), encoding="utf-8")

    assert contracts.service_rebind_marker(marker, runtime_root=runtime) is False
    assert "owner_token is stale" in marker.with_name(
        marker.name + ".rejected.reason"
    ).read_text(encoding="utf-8")


def test_rebind_commit_io_failure_leaves_marker_pending_for_retry(
    runtime,
    monkeypatch,
):
    artifact, _v1, v3 = _seed_three_versions(runtime)
    marker = _marker(runtime, artifact, v3, rebind_id="retry-after-io")

    def fail_commit(*_args, **_kwargs):
        raise OSError("simulated checkpoint I/O failure")

    monkeypatch.setattr(contracts.executor, "record_contract_rebind", fail_commit)

    with pytest.raises(OSError, match="simulated checkpoint I/O failure"):
        contracts.service_rebind_marker(marker, runtime_root=runtime)

    assert marker.exists()
    assert not marker.with_name(marker.name + ".rejected").exists()
