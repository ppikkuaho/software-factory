"""Wave-A black-box proofs for typed references and provenance anchors."""

from __future__ import annotations

import hashlib
import json
import pytest

from conftest import (
    Sandbox,
    finalize_gate_decision,
    seed_mrec_candidate,
    transcribe_engine_screen,
)
from ht import schemas
from ht.errors import HtError, HtUsageError
from ht.paths import Root
from ht.references import (
    ADJUDICATION_HEADER_PREFIX,
    canonical_json_sha256,
    parse_ref,
    resolve_ref,
)


def _mint_issue(sb: Sandbox, title: str) -> str:
    result = sb.run(
        "issue", "mint",
        "--title", title,
        "--question", f"{title}-QUESTION",
        "--done-definition", f"{title}-DONE",
        "--provenance", f"user-seed#{title.lower()}",
        "--lanes", "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr
    issue_id = f"I-{len(list((sb.root / 'tier1/issues').glob('I-*.json')))}"
    assert sb.run(
        "issue", "ratify", "--issue", issue_id, role="user"
    ).returncode == 0
    assert sb.run(
        "issue", "activate", "--issue", issue_id, role="user"
    ).returncode == 0
    return issue_id


def _seed_affiliated_candidate(sb: Sandbox) -> None:
    assert _mint_issue(sb, "SYNTHETIC-WAVE-A") == "I-1"
    assert sb.run(
        "tree", "init", "L4", "--root-question", "SYNTHETIC-ROOT",
        role="director",
    ).returncode == 0
    assert sb.run(
        "node", "mint", "--tree", "L4", "--root",
        "--premise", "SYNTHETIC-PREMISE", "--from-issue", "I-1",
        "--rationale", "SYNTHETIC-RATIONALE", role="director",
    ).returncode == 0
    assert sb.run(
        "dispatch", "create", "--tree", "L4", "--node", "1",
        "--question", "SYNTHETIC-DISPATCH",
        "--done-definition", "SYNTHETIC-DISPATCH-DONE",
        "--issue-ref", "I-1", role="director",
    ).returncode == 0
    source = sb.root.parent / "wave-a-report.md"
    source.write_text("# SYNTHETIC WAVE-A REPORT\nline two\n", encoding="utf-8")
    assert sb.run(
        "report", "submit", "--tree", "L4", "--dispatch", "d-1-1",
        "--src", str(source), role="unit",
    ).returncode == 0
    assert sb.run(
        "claim", "grant", "--tree", "L4", "--dispatch", "d-1-1",
        "--text", "SYNTHETIC-WAVE-A-CLAIM",
        "--proposed-tier", "2", "--granted-tier", "1",
        "--reason", "SYNTHETIC-DEMOTION-REASON",
        "--standing-class", "trunk",
        "--anchor", "trees/L4/nodes/1/reports/d-1-1-report.md:1:1",
        role="verifier",
    ).returncode == 0


def test_strict_reference_grammar_wrong_type_ambiguity_and_report_hash(
    sandbox: Sandbox,
) -> None:
    assert parse_ref("ledger-entry#L-7").canonical == "ledger#L-7"
    for malformed in (
        "MR-1",
        "observatory/report-card.md",
        "dispatch#d-1-1: completed",
        "observatory-report#../escape",
        " tree#L4/node#1",
    ):
        with pytest.raises(HtUsageError):
            parse_ref(malformed)

    l4_adj = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    l5_adj = seed_mrec_candidate(sandbox, "tree#L5/node#1")
    assert l4_adj.endswith("d-1-1-a1") and l5_adj.endswith("d-1-1-a1")
    with pytest.raises(HtUsageError, match=r"ambiguous.*tree#L4/dispatch.*tree#L5/dispatch"):
        resolve_ref(Root(sandbox.root), "dispatch#d-1-1")
    with pytest.raises(HtUsageError, match="has type node; expected claim"):
        resolve_ref(
            Root(sandbox.root), "tree#L4/node#1", expected={"claim"}
        )

    report = resolve_ref(Root(sandbox.root), "tree#L4/report#d-1-1")
    assert report.metadata["sha256"] == hashlib.sha256(report.path.read_bytes()).hexdigest()
    report.path.write_text("TAMPERED-SYNTHETIC-BYTES\n", encoding="utf-8")
    with pytest.raises(HtError, match="hash mismatch"):
        resolve_ref(Root(sandbox.root), "tree#L4/report#d-1-1")

    escaped = sandbox.root.parent / "escaped-report-card.md"
    escaped.write_text("# SYNTHETIC ESCAPE TARGET\n", encoding="utf-8")
    canonical = sandbox.root / "readout/observatory/escape-run/report-card.md"
    canonical.parent.mkdir(parents=True)
    canonical.symlink_to(escaped)
    with pytest.raises(HtError, match="escapes the research root"):
        resolve_ref(Root(sandbox.root), "observatory-report#escape-run")


def test_closure_ref_failures_are_nonmutating_and_name_ownership(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    assert _mint_issue(sandbox, "SYNTHETIC-OTHER") == "I-2"
    sb = sandbox
    assert sb.run(
        "tree", "init", "L5", "--root-question", "SYNTHETIC-L5",
        role="director",
    ).returncode == 0
    assert sb.run(
        "node", "mint", "--tree", "L5", "--root",
        "--premise", "SYNTHETIC-OTHER-PREMISE", "--from-issue", "I-2",
        "--rationale", "SYNTHETIC-OTHER-RATIONALE", role="director",
    ).returncode == 0
    assert sb.run(
        "dispatch", "create", "--tree", "L5", "--node", "1",
        "--question", "SYNTHETIC-OTHER-DISPATCH",
        "--done-definition", "SYNTHETIC-OTHER-DONE", "--issue-ref", "I-2",
        role="director",
    ).returncode == 0
    before = (sb.root / "tier1/issues/I-1.json").read_bytes()
    for ref, expected in (
        ("ledger#L-999", "no such ledger"),
        ("tree#L5/dispatch#d-1-1", "issue#I-2, not issue#I-1"),
        ("dispatch#d-1-1", "ambiguous"),
        ("dispatch#d-1-1: completed", "malformed typed reference"),
    ):
        rejected = sb.run(
            "issue", "close", "--issue", "I-1", "--text", "SYNTHETIC-CLOSE",
            "--ref", ref, role="pc",
        )
        assert rejected.returncode != 0
        assert expected in rejected.stderr
        assert (sb.root / "tier1/issues/I-1.json").read_bytes() == before


def test_observatory_registration_attachment_and_hook_write_once(
    sandbox: Sandbox,
) -> None:
    _mint_issue(sandbox, "SYNTHETIC-OBS")
    producer = sandbox.root.parent / "producer-report-card.md"
    producer.write_bytes(b"# SYNTHETIC OBSERVATORY REPORT CARD\n")
    registered = sandbox.run(
        "observatory", "register", "--run-id", "synthetic-run-1",
        "--report-card", str(producer), role="harness",
    )
    assert registered.returncode == 0, registered.stderr
    canonical = sandbox.root / "readout/observatory/synthetic-run-1/report-card.md"
    assert canonical.read_bytes() == producer.read_bytes()
    attached = sandbox.run(
        "issue", "observatory-attach", "--issue", "I-1",
        "--ref", "observatory-report#synthetic-run-1", role="pc",
    )
    assert attached.returncode == 0, attached.stderr
    attachment = sandbox.load("tier1/issues/I-1.json")["observatory_attachments"]
    assert attachment == [
        {
            "ref": "observatory-report#synthetic-run-1",
            "sha256": hashlib.sha256(producer.read_bytes()).hexdigest(),
        }
    ]
    assert sandbox.git("status", "--porcelain").stdout == ""

    canonical.write_bytes(canonical.read_bytes() + b"tamper\n")
    sandbox.git("add", "--", canonical.relative_to(sandbox.root).as_posix())
    rejected = sandbox.git(
        "commit", "-m", "tamper report",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert rejected.returncode != 0
    assert "write-once" in rejected.stderr


@pytest.mark.parametrize("operation", ["delete", "rename", "recreate"])
def test_observatory_hook_rejects_delete_rename_and_historical_recreation(
    sandbox: Sandbox,
    operation: str,
) -> None:
    producer = sandbox.root.parent / f"{operation}-producer-card.md"
    producer.write_bytes(b"# SYNTHETIC WRITE-ONCE CARD\n")
    assert sandbox.run(
        "observatory", "register", "--run-id", "write-once-run",
        "--report-card", str(producer), role="harness",
    ).returncode == 0
    rel = "readout/observatory/write-once-run/report-card.md"
    if operation == "delete":
        assert sandbox.git("rm", "--", rel).returncode == 0
    elif operation == "rename":
        assert sandbox.git(
            "mv", "--", rel, "readout/observatory/write-once-run/moved.md"
        ).returncode == 0
    else:
        assert sandbox.git("rm", "--", rel).returncode == 0
        deleted = sandbox.git("commit", "--no-verify", "-m", "synthetic deletion")
        assert deleted.returncode == 0, deleted.stderr
        path = sandbox.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(producer.read_bytes())
        assert sandbox.git("add", "--", rel).returncode == 0
    rejected = sandbox.git(
        "commit", "-m", f"synthetic {operation}",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert rejected.returncode != 0
    if operation == "recreate":
        assert "re-creates" in rejected.stderr
    else:
        assert "write-once" in rejected.stderr


def test_mrec_provenance_is_exact_and_claim_drift_fails_closed(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    assert sandbox.run(
        "dispatch", "outcome", "--tree", "L4", "--dispatch", "d-1-1",
        "--outcome", "completed", role="harness",
    ).returncode == 0
    created = sandbox.run(
        "mrec", "create", "--candidate-ref", "tree#L4/node#1",
        "--lane-verdict", "SYNTHETIC-LANE-OUTCOME",
        "--lane-adjudication-ref", "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane", "L4", role="harness",
    )
    assert created.returncode == 0, created.stderr
    viewed = sandbox.run("mrec", "provenance", "MR-1", "--json")
    assert viewed.returncode == 0, viewed.stderr
    chain = json.loads(viewed.stdout)
    assert chain["status"] == "resolved"
    assert chain["candidate_ref"] == "tree#L4/node#1"
    assert chain["issue_ref"] == "issue#I-1"
    assert chain["dispatch_refs"] == ["tree#L4/dispatch#d-1-1"]
    assert chain["adjudication_refs"] == [
        "tree#L4/adjudication#d-1-1-a1"
    ]

    transcribed = transcribe_engine_screen(sandbox, "MR-1")
    assert transcribed.returncode == 0, transcribed.stderr
    finalize_gate_decision(sandbox, "MR-1")
    assert sandbox.run(
        "claim", "revalidate", "c-1-1", "--tree", "L4",
        "--ref", "synthetic-revalidation#wave-a", role="verifier",
    ).returncode == 0
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    rejected = sandbox.run(
        "node", "merge", "--tree", "L4", "--node", "1",
        "--merge-record", "MR-1", role="director",
    )
    assert rejected.returncode != 0
    assert "backing claim" in rejected.stderr and "hash drifted" in rejected.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    failed_view = sandbox.run("mrec", "provenance", "MR-1", "--json")
    assert failed_view.returncode != 0
    assert "drifted" in failed_view.stderr


def test_resolver_confines_tree_ledger_and_symlink_paths(sandbox: Sandbox) -> None:
    _seed_affiliated_candidate(sandbox)
    root = Root(sandbox.root)
    escaped = sandbox.root / "nodes/1/node.json"
    escaped.parent.mkdir(parents=True)
    escaped.write_bytes((sandbox.root / "trees/L4/nodes/1/node.json").read_bytes())
    for ref in ("tree#../node#1", "tree#./node#1"):
        with pytest.raises(HtUsageError, match="canonical tree"):
            resolve_ref(root, ref)

    hidden_ledger = sandbox.root / "ledger/top/research/nested/L-999.json"
    hidden_ledger.parent.mkdir(parents=True)
    hidden_ledger.write_text('{"id":"L-999"}\n', encoding="utf-8")
    with pytest.raises(HtUsageError, match="no such ledger"):
        resolve_ref(root, "ledger#L-999")

    outside_tree = sandbox.root.parent / "outside-tree"
    (outside_tree / "nodes/1").mkdir(parents=True)
    (outside_tree / "tree.json").write_text(
        json.dumps({"component": "escape"}), encoding="utf-8"
    )
    (outside_tree / "nodes/1/node.json").write_bytes(escaped.read_bytes())
    (sandbox.root / "trees/escape").symlink_to(outside_tree, target_is_directory=True)
    with pytest.raises(HtUsageError, match="canonical tree"):
        resolve_ref(root, "tree#escape/node#1")

    external_node = sandbox.root.parent / "external-node.json"
    external_node.write_bytes(escaped.read_bytes())
    canonical_node = sandbox.root / "trees/L4/nodes/1/node.json"
    canonical_node.unlink()
    canonical_node.symlink_to(external_node)
    with pytest.raises(HtError, match="symlink"):
        resolve_ref(root, "tree#L4/node#1")


def test_legacy_adjudication_path_normalizes_read_only(sandbox: Sandbox) -> None:
    _seed_affiliated_candidate(sandbox)
    dispatch_path = sandbox.root / "trees/L4/nodes/1/dispatches/d-1-1.json"
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    dispatch["adjudications"] = [
        "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    ]
    dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")
    resolved = resolve_ref(
        Root(sandbox.root),
        "trees/L4/nodes/1/adjudications/d-1-1-a1.md",
        expected={"adjudication"},
    )
    assert resolved.canonical == "tree#L4/adjudication#d-1-1-a1"


def test_direct_issue_and_merge_record_refs_reject_symlinked_state(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    created = sandbox.run(
        "mrec", "create", "--candidate-ref", "tree#L4/node#1",
        "--lane-verdict", "SYNTHETIC LANE PASS",
        "--lane-adjudication-ref", "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane", "L4", role="harness",
    )
    assert created.returncode == 0, created.stderr
    for relative, ref in (
        ("tier1/issues/I-1.json", "issue#I-1"),
        ("tier1/merge-records/MR-1.json", "merge-record#MR-1"),
    ):
        path = sandbox.root / relative
        external = sandbox.root.parent / (path.name + ".external")
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
        with pytest.raises(HtError, match="symlink"):
            resolve_ref(Root(sandbox.root), ref)


def test_adjudication_headers_reject_duplicate_nonfinite_and_forged_fields(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    path = sandbox.root / "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    original = path.read_text(encoding="utf-8")
    first, body = original.split("\n", 1)
    header = json.loads(first.removeprefix(ADJUDICATION_HEADER_PREFIX))
    compact = json.dumps(header, sort_keys=True, separators=(",", ":"))
    duplicate = compact.replace(
        f'"epoch":{header["epoch"]}',
        f'"epoch":{header["epoch"]},"epoch":{header["epoch"]}',
    )
    nonfinite = compact.replace(f'"epoch":{header["epoch"]}', '"epoch":NaN')
    variants = [
        duplicate,
        nonfinite,
        json.dumps(
            {**header, "claim_ref": "tree#L4/claim#c-1-99"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {**header, "verdict": "rejected"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {**header, "granted_tier": 99},
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    for payload in variants:
        path.write_text(ADJUDICATION_HEADER_PREFIX + payload + "\n" + body)
        with pytest.raises(HtError):
            resolve_ref(
                Root(sandbox.root),
                "tree#L4/adjudication#d-1-1-a1",
            )
    path.write_text(original, encoding="utf-8")


@pytest.mark.parametrize("operation", ["grant", "reject"])
def test_adjudication_creation_rejects_preexisting_next_ordinal_nonmutating(
    sandbox: Sandbox,
    operation: str,
) -> None:
    _seed_affiliated_candidate(sandbox)
    record = sandbox.root / "trees/L4/nodes/1/adjudications/d-1-1-a2.md"
    record.write_bytes(b"SYNTHETIC PREEXISTING ADJUDICATION SENTINEL\n")
    dispatch = sandbox.root / "trees/L4/nodes/1/dispatches/d-1-1.json"
    node = sandbox.root / "trees/L4/nodes/1/node.json"
    before = (record.read_bytes(), dispatch.read_bytes(), node.read_bytes())
    if operation == "grant":
        result = sandbox.run(
            "claim", "grant", "--tree", "L4", "--dispatch", "d-1-1",
            "--text", "SYNTHETIC SECOND CLAIM", "--proposed-tier", "1",
            "--granted-tier", "1", "--standing-class", "trunk",
            "--anchor", "trees/L4/nodes/1/reports/d-1-1-report.md:1:1",
            role="verifier",
        )
    else:
        result = sandbox.run(
            "claim", "reject", "--tree", "L4", "--dispatch", "d-1-1",
            "--text", "SYNTHETIC REJECTED CLAIM", "--reason", "SYNTHETIC REASON",
            role="verifier",
        )
    assert result.returncode != 0
    assert "already exists" in result.stderr and "write-once" in result.stderr
    assert (record.read_bytes(), dispatch.read_bytes(), node.read_bytes()) == before


def test_adjudication_creation_rejects_symlinked_parent_without_escape(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    parent = sandbox.root / "trees/L4/nodes/1/adjudications"
    preserved = sandbox.root / "trees/L4/nodes/1/adjudications-preserved"
    parent.rename(preserved)
    external = sandbox.root.parent / "external-adjudications"
    external.mkdir()
    parent.symlink_to(external, target_is_directory=True)
    dispatch = sandbox.root / "trees/L4/nodes/1/dispatches/d-1-1.json"
    node = sandbox.root / "trees/L4/nodes/1/node.json"
    before = (dispatch.read_bytes(), node.read_bytes(), tuple(external.iterdir()))
    rejected = sandbox.run(
        "claim", "reject", "--tree", "L4", "--dispatch", "d-1-1",
        "--text", "SYNTHETIC REJECTED CLAIM", "--reason", "SYNTHETIC REASON",
        role="verifier",
    )
    assert rejected.returncode != 0
    assert "symlinked ancestor" in rejected.stderr
    assert (dispatch.read_bytes(), node.read_bytes(), tuple(external.iterdir())) == before


@pytest.mark.parametrize("operation", ["grant", "reject"])
@pytest.mark.parametrize("drift", ["report-bytes", "report-ref"])
def test_adjudication_creation_rechecks_frozen_report_nonmutating(
    sandbox: Sandbox,
    operation: str,
    drift: str,
) -> None:
    _seed_affiliated_candidate(sandbox)
    report = sandbox.root / "trees/L4/nodes/1/reports/d-1-1-report.md"
    dispatch = sandbox.root / "trees/L4/nodes/1/dispatches/d-1-1.json"
    node = sandbox.root / "trees/L4/nodes/1/node.json"
    if drift == "report-bytes":
        report.write_bytes(report.read_bytes() + b"SYNTHETIC DRIFT\n")
    else:
        document = json.loads(dispatch.read_text(encoding="utf-8"))
        document["report_ref"] = "trees/L4/nodes/1/reports/wrong-report.md"
        dispatch.write_text(json.dumps(document), encoding="utf-8")
    before = (report.read_bytes(), dispatch.read_bytes(), node.read_bytes())
    if operation == "grant":
        result = sandbox.run(
            "claim", "grant", "--tree", "L4", "--dispatch", "d-1-1",
            "--text", "SYNTHETIC SECOND CLAIM", "--proposed-tier", "1",
            "--granted-tier", "1", "--standing-class", "trunk",
            "--anchor", "trees/L4/nodes/1/reports/d-1-1-report.md:1:1",
            role="verifier",
        )
    else:
        result = sandbox.run(
            "claim", "reject", "--tree", "L4", "--dispatch", "d-1-1",
            "--text", "SYNTHETIC REJECTED CLAIM", "--reason", "SYNTHETIC REASON",
            role="verifier",
        )
    assert result.returncode != 0
    assert (report.read_bytes(), dispatch.read_bytes(), node.read_bytes()) == before
    assert not (
        sandbox.root / "trees/L4/nodes/1/adjudications/d-1-1-a2.md"
    ).exists()


def test_observatory_registration_confines_parent_and_history(
    sandbox: Sandbox,
) -> None:
    source = sandbox.root.parent / "producer-observatory-card.md"
    source.write_bytes(b"# SYNTHETIC OBSERVATORY CARD\n")
    external = sandbox.root.parent / "external-observatory-target"
    external.mkdir()
    run_parent = sandbox.root / "readout/observatory/escape-run"
    run_parent.parent.mkdir(parents=True, exist_ok=True)
    run_parent.symlink_to(external, target_is_directory=True)
    rejected = sandbox.run(
        "observatory", "register", "--run-id", "escape-run",
        "--report-card", str(source), role="harness",
    )
    assert rejected.returncode != 0
    assert not (external / "report-card.md").exists()

    run_parent.unlink()
    accepted = sandbox.run(
        "observatory", "register", "--run-id", "history-run",
        "--report-card", str(source), role="harness",
    )
    assert accepted.returncode == 0, accepted.stderr
    relative = "readout/observatory/history-run/report-card.md"
    assert sandbox.git("rm", "--", relative).returncode == 0
    deleted = sandbox.git("commit", "--no-verify", "-m", "synthetic deletion")
    assert deleted.returncode == 0, deleted.stderr
    before = (
        sandbox.git("rev-parse", "HEAD").stdout,
        sandbox.git("status", "--porcelain=v1").stdout,
    )
    retried = sandbox.run(
        "observatory", "register", "--run-id", "history-run",
        "--report-card", str(source), role="harness",
    )
    assert retried.returncode != 0
    assert "historical path" in retried.stderr
    assert not (sandbox.root / relative).exists()
    assert (
        sandbox.git("rev-parse", "HEAD").stdout,
        sandbox.git("status", "--porcelain=v1").stdout,
    ) == before


def test_observatory_attachment_rejects_assume_unchanged_blob_drift(
    sandbox: Sandbox,
) -> None:
    _mint_issue(sandbox, "SYNTHETIC-OBS-DRIFT")
    source = sandbox.root.parent / "assume-unchanged-card.md"
    source.write_bytes(b"# SYNTHETIC COMMITTED CARD\n")
    assert sandbox.run(
        "observatory", "register", "--run-id", "assume-run",
        "--report-card", str(source), role="harness",
    ).returncode == 0
    relative = "readout/observatory/assume-run/report-card.md"
    assert sandbox.git("update-index", "--assume-unchanged", relative).returncode == 0
    canonical = sandbox.root / relative
    canonical.write_bytes(b"# SYNTHETIC HIDDEN DRIFT\n")
    issue = sandbox.root / "tier1/issues/I-1.json"
    before = issue.read_bytes()
    rejected = sandbox.run(
        "issue", "observatory-attach", "--issue", "I-1",
        "--ref", "observatory-report#assume-run", role="pc",
    )
    assert rejected.returncode != 0
    assert "Git blob identities" in rejected.stderr
    assert issue.read_bytes() == before


def test_observatory_attachment_rejects_index_head_mismatch(
    sandbox: Sandbox,
) -> None:
    _mint_issue(sandbox, "SYNTHETIC-OBS-INDEX-DRIFT")
    source = sandbox.root.parent / "index-drift-card.md"
    source.write_bytes(b"# SYNTHETIC COMMITTED INDEX CARD\n")
    assert sandbox.run(
        "observatory", "register", "--run-id", "index-run",
        "--report-card", str(source), role="harness",
    ).returncode == 0
    relative = "readout/observatory/index-run/report-card.md"
    canonical = sandbox.root / relative
    committed_bytes = canonical.read_bytes()
    canonical.write_bytes(b"# SYNTHETIC STAGED INDEX DRIFT\n")
    assert sandbox.git("add", "--", relative).returncode == 0
    canonical.write_bytes(committed_bytes)
    issue = sandbox.root / "tier1/issues/I-1.json"
    before = issue.read_bytes()
    rejected = sandbox.run(
        "issue", "observatory-attach", "--issue", "I-1",
        "--ref", "observatory-report#index-run", role="pc",
    )
    assert rejected.returncode != 0
    assert "Git blob identities" in rejected.stderr
    assert issue.read_bytes() == before


def test_report_historical_path_resubmission_is_nonmutating(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    relative = "trees/L4/nodes/1/reports/d-1-1-report.md"
    dispatch_relative = "trees/L4/nodes/1/dispatches/d-1-1.json"
    assert sandbox.git("rm", "--", relative).returncode == 0
    dispatch_path = sandbox.root / dispatch_relative
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    dispatch["report_ref"] = None
    dispatch["report_hash"] = None
    dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")
    assert sandbox.git("add", "--", dispatch_relative).returncode == 0
    deleted = sandbox.git(
        "commit", "--no-verify", "-m", "synthetic report history reset"
    )
    assert deleted.returncode == 0, deleted.stderr
    source = sandbox.root.parent / "historical-resubmit.md"
    source.write_bytes(b"# SYNTHETIC HISTORICAL RESUBMIT\n")
    before = (
        sandbox.git("rev-parse", "HEAD").stdout,
        sandbox.git("status", "--porcelain=v1").stdout,
        dispatch_path.read_bytes(),
    )
    rejected = sandbox.run(
        "report", "submit", "--tree", "L4", "--dispatch", "d-1-1",
        "--src", str(source), role="unit",
    )
    assert rejected.returncode != 0
    assert "used historically" in rejected.stderr
    assert not (sandbox.root / relative).exists()
    assert (
        sandbox.git("rev-parse", "HEAD").stdout,
        sandbox.git("status", "--porcelain=v1").stdout,
        dispatch_path.read_bytes(),
    ) == before


@pytest.mark.parametrize("missing", ["lane_adjudication_ref", "backing_claims"])
def test_partial_merge_record_anchor_is_schema_invalid_and_provenance_fails(
    sandbox: Sandbox,
    missing: str,
) -> None:
    adjudication = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    created = sandbox.run(
        "mrec", "create", "--candidate-ref", "tree#L4/node#1",
        "--lane-verdict", "SYNTHETIC LANE PASS",
        "--lane-adjudication-ref", adjudication, "--scope-lane", "L4",
        role="harness",
    )
    assert created.returncode == 0, created.stderr
    path = sandbox.root / "tier1/merge-records/MR-1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop(missing)
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(HtError, match="schema-nonconforming merge_record"):
        schemas.validate(sandbox.root / "system/schemas", "merge_record", record)
    viewed = sandbox.run("mrec", "provenance", "MR-1", "--json")
    assert viewed.returncode != 0
    assert "partial provenance anchor" in viewed.stderr


def test_future_epoch_revalidation_is_merge_ineligible(sandbox: Sandbox) -> None:
    _seed_affiliated_candidate(sandbox)
    node_path = sandbox.root / "trees/L4/nodes/1/node.json"
    node = json.loads(node_path.read_text(encoding="utf-8"))
    node["claims"][0]["standing_class"] = "sandbox"
    node["claims"][0]["revalidation"] = {
        "date": "2026-07-14",
        "epoch": 99,
        "ref": "tree#L4/adjudication#d-1-1-a1",
    }
    node_path.write_text(json.dumps(node), encoding="utf-8")
    rejected = sandbox.run(
        "mrec", "create", "--candidate-ref", "tree#L4/node#1",
        "--lane-verdict", "SYNTHETIC LANE PASS",
        "--lane-adjudication-ref", "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane", "L4", role="harness",
    )
    assert rejected.returncode != 0
    assert "merge-ineligible granted claims: c-1-1" in rejected.stderr
    assert not (sandbox.root / "tier1/merge-records/MR-1.json").exists()


def test_merge_record_rejects_backing_adjudication_from_other_node(
    sandbox: Sandbox,
) -> None:
    first = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    created = sandbox.run(
        "mrec", "create", "--candidate-ref", "tree#L4/node#1",
        "--lane-verdict", "SYNTHETIC LANE PASS",
        "--lane-adjudication-ref", first, "--scope-lane", "L4",
        role="harness",
    )
    assert created.returncode == 0, created.stderr
    second = seed_mrec_candidate(sandbox, "tree#L4/node#2")
    path = sandbox.root / "tier1/merge-records/MR-1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["backing_claims"][0]["adjudication_ref"] = second
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(HtError, match="backing"):
        resolve_ref(Root(sandbox.root), "merge-record#MR-1")


def test_issue_close_rejects_cross_issue_backing_adjudication_nonmutating(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    created = sandbox.run(
        "mrec", "create", "--candidate-ref", "tree#L4/node#1",
        "--lane-verdict", "SYNTHETIC LANE PASS",
        "--lane-adjudication-ref", "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane", "L4", role="harness",
    )
    assert created.returncode == 0, created.stderr
    assert _mint_issue(sandbox, "SYNTHETIC-CROSS-ISSUE") == "I-2"
    assert sandbox.run(
        "node", "mint", "--tree", "L4", "--root",
        "--premise", "SYNTHETIC I-2 NODE", "--from-issue", "I-2",
        "--rationale", "SYNTHETIC CROSS-ISSUE FIXTURE", role="director",
    ).returncode == 0
    assert sandbox.run(
        "dispatch", "create", "--tree", "L4", "--node", "2",
        "--question", "SYNTHETIC I-2 DISPATCH", "--done-definition", "SYNTHETIC DONE",
        "--issue-ref", "I-2", role="director",
    ).returncode == 0
    source = sandbox.root.parent / "cross-issue-report.md"
    source.write_text("# SYNTHETIC CROSS ISSUE REPORT\n", encoding="utf-8")
    assert sandbox.run(
        "report", "submit", "--tree", "L4", "--dispatch", "d-2-1",
        "--src", str(source), role="unit",
    ).returncode == 0
    assert sandbox.run(
        "claim", "grant", "--tree", "L4", "--dispatch", "d-2-1",
        "--text", "SYNTHETIC I-2 CLAIM", "--proposed-tier", "1",
        "--granted-tier", "1", "--standing-class", "trunk",
        "--anchor", "trees/L4/nodes/2/reports/d-2-1-report.md:1:1",
        role="verifier",
    ).returncode == 0
    claim = sandbox.load("trees/L4/nodes/2/node.json")["claims"][0]
    record_path = sandbox.root / "tier1/merge-records/MR-1.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["backing_claims"] = [
        {
            "ref": "tree#L4/claim#c-2-1",
            "sha256": canonical_json_sha256(claim),
            "adjudication_ref": "tree#L4/adjudication#d-2-1-a1",
        }
    ]
    record_path.write_text(json.dumps(record), encoding="utf-8")
    issue_path = sandbox.root / "tier1/issues/I-1.json"
    before = issue_path.read_bytes()
    rejected = sandbox.run(
        "issue", "close", "--issue", "I-1", "--text", "SYNTHETIC CLOSE",
        "--ref", "merge-record#MR-1", role="pc",
    )
    assert rejected.returncode != 0
    assert "outside its candidate" in rejected.stderr or "conflicting issue" in rejected.stderr
    assert issue_path.read_bytes() == before


def test_dispatch_path_document_identity_mismatch_rejects(sandbox: Sandbox) -> None:
    _seed_affiliated_candidate(sandbox)
    path = sandbox.root / "trees/L4/nodes/1/dispatches/d-1-1.json"
    dispatch = json.loads(path.read_text(encoding="utf-8"))
    dispatch["node"] = "2"
    path.write_text(json.dumps(dispatch), encoding="utf-8")
    with pytest.raises(HtError, match="path/document identity mismatch"):
        resolve_ref(Root(sandbox.root), "tree#L4/dispatch#d-1-1")


def test_issue_and_merge_record_document_ids_must_match_paths(
    sandbox: Sandbox,
) -> None:
    _seed_affiliated_candidate(sandbox)
    assert sandbox.run(
        "mrec", "create", "--candidate-ref", "tree#L4/node#1",
        "--lane-verdict", "SYNTHETIC LANE PASS",
        "--lane-adjudication-ref", "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane", "L4", role="harness",
    ).returncode == 0
    for relative, ref, forged_id in (
        ("tier1/issues/I-1.json", "issue#I-1", "I-99"),
        ("tier1/merge-records/MR-1.json", "merge-record#MR-1", "MR-99"),
    ):
        path = sandbox.root / relative
        original = path.read_bytes()
        document = json.loads(original)
        document["id"] = forged_id
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(HtError, match="contains id"):
            resolve_ref(Root(sandbox.root), ref)
        path.write_bytes(original)


@pytest.mark.parametrize("kind", ["adjudication", "report"])
@pytest.mark.parametrize("operation", ["delete", "rename", "recreate", "replace"])
def test_hook_enforces_full_write_once_path_identity(
    sandbox: Sandbox,
    kind: str,
    operation: str,
) -> None:
    _seed_affiliated_candidate(sandbox)
    if kind == "adjudication":
        relative = "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
        moved = "trees/L4/nodes/1/adjudications/moved.md"
        role = "verifier"
    else:
        relative = "trees/L4/nodes/1/reports/d-1-1-report.md"
        moved = "trees/L4/nodes/1/reports/moved-report.md"
        role = "unit"
    path = sandbox.root / relative
    original = path.read_bytes()
    if operation == "delete":
        assert sandbox.git("rm", "--", relative).returncode == 0
    elif operation == "rename":
        assert sandbox.git("mv", "--", relative, moved).returncode == 0
    elif operation == "recreate":
        assert sandbox.git("rm", "--", relative).returncode == 0
        deleted = sandbox.git("commit", "--no-verify", "-m", "synthetic deletion")
        assert deleted.returncode == 0, deleted.stderr
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
        assert sandbox.git("add", "--", relative).returncode == 0
    else:
        external = sandbox.root.parent / f"external-{kind}.md"
        external.write_bytes(original)
        path.unlink()
        path.symlink_to(external)
        assert sandbox.git("add", "--", relative).returncode == 0
    rejected = sandbox.git(
        "commit", "-m", f"synthetic {operation} {kind}",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": role},
    )
    assert rejected.returncode != 0
    assert "write-once" in rejected.stderr or "frozen" in rejected.stderr


def test_hook_rejects_first_add_observatory_symlink(sandbox: Sandbox) -> None:
    external = sandbox.root.parent / "external-first-add-card.md"
    external.write_bytes(b"# SYNTHETIC EXTERNAL CARD\n")
    relative = "readout/observatory/symlink-add/report-card.md"
    path = sandbox.root / relative
    path.parent.mkdir(parents=True)
    path.symlink_to(external)
    assert sandbox.git("add", "--", relative).returncode == 0
    rejected = sandbox.git(
        "commit", "-m", "synthetic symlink observatory add",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert rejected.returncode != 0
    assert "100644 regular blob" in rejected.stderr


def test_hook_rejects_observatory_type_replacement(sandbox: Sandbox) -> None:
    source = sandbox.root.parent / "type-replacement-card.md"
    source.write_bytes(b"# SYNTHETIC OBSERVATORY TYPE TARGET\n")
    assert sandbox.run(
        "observatory", "register", "--run-id", "type-run",
        "--report-card", str(source), role="harness",
    ).returncode == 0
    relative = "readout/observatory/type-run/report-card.md"
    canonical = sandbox.root / relative
    external = sandbox.root.parent / "external-type-card.md"
    external.write_bytes(canonical.read_bytes())
    canonical.unlink()
    canonical.symlink_to(external)
    assert sandbox.git("add", "--", relative).returncode == 0
    rejected = sandbox.git(
        "commit", "-m", "synthetic observatory type replacement",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert rejected.returncode != 0
    assert "write-once" in rejected.stderr


def test_hook_rejects_first_add_adjudication_symlink(sandbox: Sandbox) -> None:
    external = sandbox.root.parent / "external-first-add-adjudication.md"
    external.write_bytes(b"SYNTHETIC EXTERNAL ADJUDICATION\n")
    relative = "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    path = sandbox.root / relative
    path.parent.mkdir(parents=True)
    path.symlink_to(external)
    assert sandbox.git("add", "--", relative).returncode == 0
    rejected = sandbox.git(
        "commit", "-m", "synthetic adjudication symlink add",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "verifier"},
    )
    assert rejected.returncode != 0
    assert "100644 regular blob" in rejected.stderr
