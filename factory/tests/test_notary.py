from __future__ import annotations

import hashlib
import json
import stat

import pytest

from harnessd import notary


def test_stamp_file_is_raw_byte_exact_and_missing_is_explicit(tmp_path):
    artifact = tmp_path / "artifact.bin"
    raw = b"\x00notary\r\n\xff"
    artifact.write_bytes(raw)

    assert notary.stamp(artifact) == {
        "present": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    assert notary.stamp(tmp_path / "missing.bin") == {"present": False}


def test_stamp_selected_file_set_is_sorted_and_legacy_canonical(tmp_path):
    root = tmp_path / "tests"
    (root / "nested").mkdir(parents=True)
    first = root / "a.py"
    second = root / "nested" / "b.py"
    first.write_text("A = 1\n", encoding="utf-8")
    second.write_text("B = 2\n", encoding="utf-8")

    stamped = notary.stamp(
        root,
        members=[second, first],
        root_label="tests",
    )

    assert list(stamped["files"]) == ["a.py", "nested/b.py"]
    assert stamped["root"] == "tests"
    assert stamped["file_count"] == 2
    canonical = json.dumps(
        stamped["files"],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert stamped["sha256"] == hashlib.sha256(canonical).hexdigest()


def test_stamp_can_snapshot_exact_bytes_and_make_source_read_only(tmp_path):
    artifact = tmp_path / "intent-spec.md"
    snapshot = tmp_path / "snapshot" / "intent-spec.md"
    artifact.write_bytes(b"# intent\r\n")

    stamped = notary.stamp(
        artifact,
        snapshot_to=snapshot,
        read_only=True,
    )

    assert snapshot.read_bytes() == b"# intent\r\n"
    assert stamped == notary.stamp(snapshot)
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o444


def test_receipt_names_holder_artifact_and_version_and_check_detects_drift(tmp_path):
    artifact = tmp_path / "brief.md"
    artifact.write_text("# brief\n", encoding="utf-8")
    stamped = notary.stamp(artifact)
    held = notary.receipt("proj/widget#exec", artifact, stamped)

    assert held == {
        "holder": "proj/widget#exec",
        "artifact": str(artifact),
        "stamp": stamped,
    }
    assert notary.check(held).ok is True

    artifact.write_text("# changed brief\n", encoding="utf-8")
    verdict = notary.check(held)

    assert verdict.ok is False
    assert {item["kind"] for item in verdict.mismatches} == {"sha256", "bytes"}


def test_check_file_set_reports_added_removed_and_changed_members(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    first = root / "a.py"
    removed = root / "b.py"
    first.write_text("A = 1\n", encoding="utf-8")
    removed.write_text("B = 1\n", encoding="utf-8")
    expected = notary.stamp(root, members=[first, removed], root_label="candidate")

    first.write_text("A = 200\n", encoding="utf-8")
    removed.unlink()
    added = root / "c.py"
    added.write_text("C = 1\n", encoding="utf-8")
    verdict = notary.check(
        expected,
        target=root,
        members=[first, added],
        root_label="candidate",
    )

    assert verdict.ok is False
    pairs = {(item["kind"], item["path"]) for item in verdict.mismatches}
    assert ("removed", "b.py") in pairs
    assert ("added", "c.py") in pairs
    assert ("sha256", "a.py") in pairs
    assert ("bytes", "a.py") in pairs


def test_check_detects_snapshot_drift_with_the_same_file_primitive(tmp_path):
    source = tmp_path / "source.py"
    snapshot = tmp_path / "snapshot.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    expected = notary.stamp(source, snapshot_to=snapshot)

    snapshot.write_text("VALUE = 999\n", encoding="utf-8")
    verdict = notary.check(expected, target=snapshot)

    assert verdict.ok is False
    assert any(item["kind"] == "sha256" for item in verdict.mismatches)


def test_stamp_rejects_a_member_outside_the_selected_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside root"):
        notary.stamp(root, members=[outside])


def _revision(path, *, target, owner, prior, current):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision_id": "revision-one",
                "contract_path": str(target),
                "owner_address": owner,
                "prior_fingerprint": prior,
                "new_fingerprint": current,
                "reason": "Client confirmed the amended intent.",
                "authored_at": "2026-07-24T10:00:00+00:00",
                "ratified_at": "2026-07-24T10:00:00+00:00",
                "channel": "intent_revision",
                "channel_evidence": {"owner_token": "token"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_restamp_requires_valid_revision_record_and_returns_lineage(tmp_path):
    artifact = tmp_path / "intent-spec.md"
    artifact.write_text("# intent\n", encoding="utf-8")
    held = notary.receipt("proj#exec", artifact, notary.stamp(artifact))

    with pytest.raises(
        notary.RevisionRecordRequired,
        match="requires an explicit revision-record artifact reference",
    ):
        notary.restamp(artifact, prior_receipt=held)

    artifact.write_text("# amended intent\n", encoding="utf-8")
    current = notary.stamp(artifact)
    revision = _revision(
        tmp_path / "intent-revision.json",
        target=artifact,
        owner="proj#exec",
        prior=held["stamp"]["sha256"],
        current=current["sha256"],
    )
    result = notary.restamp(
        artifact,
        prior_receipt=held,
        revision_record_ref=revision,
    )

    assert result["stamp"] == current
    assert result["lineage"] == {
        "revision_id": "revision-one",
        "revision_record_ref": str(revision),
        "prior_fingerprint": held["stamp"]["sha256"],
        "new_fingerprint": current["sha256"],
    }
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o444


def test_restamp_refuses_wrong_prior_and_wrong_new_fingerprints(tmp_path):
    artifact = tmp_path / "brief.md"
    artifact.write_text("v1\n", encoding="utf-8")
    held = notary.receipt("proj/child#exec", artifact, notary.stamp(artifact))
    artifact.write_text("v2\n", encoding="utf-8")
    current = notary.stamp(artifact)
    revision = _revision(
        tmp_path / "revision.json",
        target=artifact,
        owner="proj#exec",
        prior="wrong-prior",
        current=current["sha256"],
    )

    with pytest.raises(notary.RevisionRecordInvalid, match="prior_fingerprint"):
        notary.restamp(
            artifact,
            prior_receipt=held,
            revision_record_ref=revision,
        )

    payload = json.loads(revision.read_text(encoding="utf-8"))
    payload["prior_fingerprint"] = held["stamp"]["sha256"]
    payload["new_fingerprint"] = "wrong-new"
    revision.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(notary.RevisionRecordInvalid, match="new_fingerprint"):
        notary.restamp(
            artifact,
            prior_receipt=held,
            revision_record_ref=revision,
        )
