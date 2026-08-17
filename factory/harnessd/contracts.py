"""Durable contract versions, holder receipts, revision lineage, and amendment ripple.

The notary proves filesystem identity.  This module supplies the domain join around it:

* one physical home and current version on the owner's binding;
* one receipt per handed contract on each holder binding;
* immutable owner-authored revision records and complete lineage;
* explicit holder-fenced rebind markers;
* one ordinary 3a message per stale live holder and revision.

No receipt state enters turn completion.  Messages wake and authorize a re-read; only a
successfully verified rebind marker advances the holder's receipt.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Iterable

from . import addressing, clock, executor, ledger, messages, notary, states, store

SCHEMA_VERSION = 1
REBINDS_DIRNAME = "contract-rebind"
REVISIONS_DIRNAME = "contract-revisions"
ACCEPTED_TESTS_REL = Path("contracts") / "accepted-tests"
AMENDMENT_TAG = "contract_amendment"

_SAFE_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ContractError(ValueError):
    """A contract receipt, revision, home, or rebind request is invalid."""


def canonical_contract_path(path: str | Path) -> str:
    """Return the stable absolute key used by owner versions and holder receipts."""
    return str(Path(path).resolve())


def receipt_fingerprint(receipt: dict) -> str | None:
    if not isinstance(receipt, dict):
        return None
    value = receipt.get("fingerprint")
    if value:
        return str(value)
    stamp = receipt.get("stamp")
    if isinstance(stamp, dict) and stamp.get("sha256"):
        return str(stamp["sha256"])
    return None


def contract_receipt(
    holder: str,
    owner_address: str,
    artifact: str | Path,
    stamped: dict,
    *,
    revision_record_ref: str | Path | None = None,
) -> dict:
    artifact_path = Path(canonical_contract_path(artifact))
    return notary.receipt(
        holder,
        artifact_path,
        stamped,
        owner_address=owner_address,
        revision_record_ref=(
            Path(revision_record_ref) if revision_record_ref is not None else None
        ),
    )


def merge_receipts(*maps_or_receipts: dict | None) -> dict:
    """Merge receipt maps/rows by canonical physical home without mutating inputs."""
    merged: dict[str, dict] = {}
    for value in maps_or_receipts:
        if not isinstance(value, dict):
            continue
        values = (
            value.values()
            if value and all(isinstance(item, dict) for item in value.values())
            and "artifact" not in value
            else (value,)
        )
        for receipt in values:
            if not isinstance(receipt, dict) or not receipt.get("artifact"):
                continue
            key = canonical_contract_path(receipt["artifact"])
            row = copy.deepcopy(receipt)
            row["artifact"] = key
            if row.get("fingerprint") is None:
                row["fingerprint"] = receipt_fingerprint(row)
            merged[key] = row
    return merged


def version_entry(
    *,
    owner_address: str,
    artifact: str | Path,
    stamped: dict,
    lineage: Iterable[dict] = (),
    revision_record_ref: str | Path | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "owner_address": str(owner_address),
        "artifact": canonical_contract_path(artifact),
        "fingerprint": stamped.get("sha256"),
        "stamp": copy.deepcopy(stamped),
        "revision_record_ref": (
            str(Path(revision_record_ref))
            if revision_record_ref is not None
            else None
        ),
        "lineage": [copy.deepcopy(row) for row in lineage],
    }


def merge_version(binding: dict, version: dict) -> dict:
    versions = copy.deepcopy(binding.get("contract_versions") or {})
    versions[canonical_contract_path(version["artifact"])] = copy.deepcopy(version)
    return versions


def accepted_test_home(
    l4_address: str,
    package_key: str,
    *,
    runtime_root: str | Path | None = None,
) -> Path:
    key = str(package_key or "").strip()
    if not _SAFE_KEY.fullmatch(key) or ".." in key:
        raise ContractError(f"accepted-test package key {package_key!r} is unsafe")
    root = runtime_root if runtime_root is not None else ledger.RUNTIME_ROOT
    if root is None:
        raise ContractError("accepted-test home requires a runtime root")
    return addressing.node_dir(l4_address, root) / ACCEPTED_TESTS_REL / key


def package_members(root: str | Path) -> list[Path]:
    """Select accepted-test contract members using the existing package exclusions."""
    path = Path(root)
    if not path.is_dir():
        return []
    members: list[Path] = []
    for member in sorted(item for item in path.rglob("*") if item.is_file()):
        if "__pycache__" in member.parts or member.suffix in {".pyc", ".pyo"}:
            continue
        if member.name == ".DS_Store":
            continue
        members.append(member)
    return members


def stamp_package(root: str | Path, *, read_only: bool = False) -> dict:
    path = Path(root)
    members = package_members(path)
    if not members:
        return {"present": False, "reason": "empty tests directory"}
    return notary.stamp(
        path,
        members=members,
        root_label="tests",
        read_only=read_only,
    )


def stage_package_home(source: str | Path, home: str | Path) -> Path:
    """Copy candidate package bytes beside the home and return the verified staging path."""
    source_path = Path(source)
    home_path = Path(home)
    members = package_members(source_path)
    if not members:
        raise ContractError(f"accepted-test candidate {source_path} is empty or absent")
    home_path.parent.mkdir(parents=True, exist_ok=True)
    stage = home_path.with_name(f".{home_path.name}.contract-stage")
    if stage.exists():
        shutil.rmtree(stage)

    def ignored(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name == "__pycache__"
            or name == ".DS_Store"
            or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(source_path, stage, ignore=ignored)
    source_stamp = stamp_package(source_path)
    stage_stamp = stamp_package(stage)
    if source_stamp.get("sha256") != stage_stamp.get("sha256"):
        shutil.rmtree(stage, ignore_errors=True)
        raise ContractError(
            f"staged accepted-test home does not match candidate {source_path}"
        )
    return stage


def install_staged_home(stage: str | Path, home: str | Path) -> dict:
    """Atomically replace one package home with a verified stage and freeze its members."""
    stage_path = Path(stage)
    home_path = Path(home)
    if home_path.exists():
        # A read-only directory's files may still be replaced by parent rename.  Keep the old home
        # as a short-lived sibling so a failed replacement can be restored.
        backup = home_path.with_name(f".{home_path.name}.contract-backup")
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(home_path, backup)
    else:
        backup = None
    try:
        os.replace(stage_path, home_path)
        stamped = stamp_package(home_path, read_only=True)
        if stamped.get("present") is not True:
            raise ContractError(f"installed accepted-test home {home_path} is empty")
    except Exception:
        if home_path.exists():
            shutil.rmtree(home_path, ignore_errors=True)
        if backup is not None and backup.exists():
            os.replace(backup, home_path)
        raise
    if backup is not None and backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    return stamped


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def revision_identity(core: dict) -> str:
    return "revision-" + hashlib.sha256(_canonical_json(core)).hexdigest()[:32]


def mint_revision_record(
    *,
    owner_address: str,
    owner_workspace: str | Path,
    contract_path: str | Path,
    prior_fingerprint: str,
    new_fingerprint: str,
    reason: str,
    channel: str,
    channel_evidence: dict,
    replacement_ref: str | Path | None = None,
) -> tuple[Path, dict]:
    """Write or recover one immutable deterministic V1 revision record."""
    reason_text = str(reason or "").strip()
    channel_text = str(channel or "").strip()
    if not reason_text:
        raise ContractError("revision reason is required")
    if not channel_text:
        raise ContractError("revision channel is required")
    if not prior_fingerprint or not new_fingerprint:
        raise ContractError("revision prior/new fingerprints are required")
    if prior_fingerprint == new_fingerprint:
        raise ContractError("revision must change the contract fingerprint")
    core = {
        "schema_version": SCHEMA_VERSION,
        "contract_path": canonical_contract_path(contract_path),
        "owner_address": str(owner_address),
        "prior_fingerprint": str(prior_fingerprint),
        "new_fingerprint": str(new_fingerprint),
        "reason": reason_text,
        "channel": channel_text,
        "channel_evidence": copy.deepcopy(channel_evidence),
    }
    if replacement_ref is not None:
        core["replacement_ref"] = str(replacement_ref)
    revision_id = revision_identity(core)
    path = Path(owner_workspace) / REVISIONS_DIRNAME / f"{revision_id}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError(f"existing revision record {path} is unreadable: {exc}") from exc
        existing_core = {
            key: existing.get(key)
            for key in core
        }
        if existing.get("revision_id") != revision_id or existing_core != core:
            raise ContractError(
                f"revision id {revision_id!r} already exists with different immutable content"
            )
        return path, existing

    now = clock.now_utc()
    record = {
        **core,
        "revision_id": revision_id,
        "authored_at": now,
        "ratified_at": now,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_replace(
        path,
        lambda handle: (
            handle.write(json.dumps(record, indent=2, sort_keys=True)),
            handle.write("\n"),
        ),
    )
    try:
        path.chmod(0o444)
    except OSError:
        pass
    return path, record


def append_lineage(version: dict, restamp_result: dict) -> dict:
    lineage = [copy.deepcopy(row) for row in version.get("lineage") or []]
    row = copy.deepcopy(restamp_result["lineage"])
    if lineage and lineage[-1].get("new_fingerprint") == row.get("new_fingerprint"):
        if lineage[-1] != row:
            raise ContractError("current contract lineage ends at this fingerprint with different data")
    else:
        expected = version.get("fingerprint")
        if row.get("prior_fingerprint") != expected:
            raise ContractError(
                "revision lineage does not start at the owner's current fingerprint: "
                f"{row.get('prior_fingerprint')!r} != {expected!r}"
            )
        lineage.append(row)
    return version_entry(
        owner_address=version["owner_address"],
        artifact=version["artifact"],
        stamped=restamp_result["stamp"],
        lineage=lineage,
        revision_record_ref=row["revision_record_ref"],
    )


def stale_receipt_holders(
    bindings: dict[str, dict] | None = None,
    *,
    include_terminal: bool = False,
) -> list[dict]:
    """Pure ledger join of owner current versions and holder receipts."""
    live = bindings if bindings is not None else ledger.all_nodes()
    found: list[dict] = []
    for holder_address, holder in live.items():
        if not include_terminal and states.is_terminal(holder.get("state")):
            continue
        receipts = holder.get("contract_receipts") or {}
        if not isinstance(receipts, dict):
            continue
        for raw_path, receipt in receipts.items():
            if not isinstance(receipt, dict):
                continue
            path = canonical_contract_path(receipt.get("artifact") or raw_path)
            owner_address = str(receipt.get("owner_address") or "")
            owner = live.get(owner_address) or {}
            version = (owner.get("contract_versions") or {}).get(path)
            if not isinstance(version, dict):
                continue
            held = receipt_fingerprint(receipt)
            current = version.get("fingerprint")
            if held and current and held != current:
                found.append({
                    "holder_address": holder_address,
                    "owner_address": owner_address,
                    "contract_path": path,
                    "held_fingerprint": held,
                    "current_fingerprint": current,
                    "revision_record_ref": version.get("revision_record_ref"),
                    "version": copy.deepcopy(version),
                })
    return sorted(
        found,
        key=lambda row: (
            row["owner_address"],
            row["holder_address"],
            row["contract_path"],
        ),
    )


def verify_rebind_chain(
    receipt: dict,
    version: dict,
    *,
    revision_record_ref: str | Path,
) -> list[dict]:
    """Verify a complete old-receipt → current-version lineage, including skipped revisions."""
    held = receipt_fingerprint(receipt)
    current = str(version.get("fingerprint") or "")
    if not held or not current:
        raise ContractError("receipt/current version is missing a fingerprint")
    if held == current:
        return []
    requested = str(Path(revision_record_ref))
    if str(version.get("revision_record_ref") or "") != requested:
        raise ContractError(
            "rebind must reference the owner's latest revision record "
            f"{version.get('revision_record_ref')!r}, got {requested!r}"
        )
    lineage = version.get("lineage") or []
    if not isinstance(lineage, list):
        raise ContractError("owner contract lineage is malformed")
    cursor = held
    used: list[dict] = []
    seen: set[str] = set()
    while cursor != current:
        if cursor in seen:
            raise ContractError(f"contract lineage loops at fingerprint {cursor}")
        seen.add(cursor)
        matches = [
            row
            for row in lineage
            if isinstance(row, dict) and row.get("prior_fingerprint") == cursor
        ]
        if len(matches) != 1:
            raise ContractError(
                "contract lineage gap after fingerprint "
                f"{cursor}: expected exactly one next revision, found {len(matches)}"
            )
        row = matches[0]
        next_fingerprint = str(row.get("new_fingerprint") or "")
        if not next_fingerprint:
            raise ContractError(
                f"contract lineage revision {row.get('revision_id')!r} has no new fingerprint"
            )
        used.append(copy.deepcopy(row))
        cursor = next_fingerprint
    if not used or str(used[-1].get("revision_record_ref") or "") != requested:
        raise ContractError(
            "contract lineage reaches the current fingerprint through a different latest "
            f"revision than {requested}"
        )
    return used


def _members_for_version(version: dict) -> list[Path] | None:
    stamp = version.get("stamp") or {}
    if not isinstance(stamp.get("files"), dict):
        return None
    return package_members(version["artifact"])


def verify_current_home(version: dict) -> None:
    target = Path(version["artifact"])
    members = _members_for_version(version)
    result = notary.check(
        version["stamp"],
        target=target,
        members=members,
        root_label="tests" if members is not None else None,
    )
    if not result:
        raise ContractError(
            f"canonical contract home {target} does not match committed fingerprint "
            f"{version.get('fingerprint')}"
        )


def _consume_marker(path: Path, *, ok: bool, reason: str = "") -> Path:
    terminal = path.with_name(path.name + (".done" if ok else ".rejected"))
    path.replace(terminal)
    if not ok and reason:
        terminal.with_name(terminal.name + ".reason").write_text(
            reason + "\n",
            encoding="utf-8",
        )
    return terminal


def service_rebind_marker(
    marker_path: str | Path,
    *,
    runtime_root: str | Path,
) -> bool:
    """Adopt one holder marker. Return True on committed/idempotent rebind."""
    path = Path(marker_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ContractError("rebind marker must be a JSON object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ContractError("rebind marker schema_version must be 1")
        rebind_id = messages.safe_message_id(payload.get("rebind_id") or path.stem)
        if path.name != f"{rebind_id}.json":
            raise ContractError("rebind marker filename must be <rebind_id>.json")
        holder_address = str(payload.get("holder") or "").strip()
        if not holder_address:
            raise ContractError("rebind marker holder is required")
        expected_dir = addressing.contract_rebind_dir(holder_address, runtime_root).resolve()
        if path.parent.resolve() != expected_dir:
            raise ContractError("rebind marker is not inside the holder's contract-rebind directory")
        holder = ledger.read_binding(holder_address)
        if holder is None:
            raise ContractError(f"rebind holder {holder_address!r} is absent")
        if holder.get("owner_token") != payload.get("owner_token"):
            raise ContractError(
                "rebind marker owner_token is stale: "
                f"{payload.get('owner_token')!r} != {holder.get('owner_token')!r}"
            )
        contract_path = canonical_contract_path(payload.get("contract_path") or "")
        receipts = holder.get("contract_receipts") or {}
        receipt = receipts.get(contract_path)
        if not isinstance(receipt, dict):
            raise ContractError(
                f"holder has no receipt for contract path {contract_path}"
            )
        owner_address = str(receipt.get("owner_address") or "")
        owner = ledger.read_binding(owner_address)
        if owner is None:
            raise ContractError(f"contract owner {owner_address!r} is absent")
        version = (owner.get("contract_versions") or {}).get(contract_path)
        if not isinstance(version, dict):
            raise ContractError(
                f"owner has no current contract version for {contract_path}"
            )
        revision_ref = str(payload.get("revision_record_ref") or "").strip()
        if not revision_ref:
            raise ContractError("rebind marker revision_record_ref is required")
        verify_rebind_chain(
            receipt,
            version,
            revision_record_ref=revision_ref,
        )
        verify_current_home(version)
        updated = contract_receipt(
            holder_address,
            owner_address,
            contract_path,
            version["stamp"],
            revision_record_ref=revision_ref,
        )
        result = executor.record_contract_rebind(
            holder_address,
            rebind_id=rebind_id,
            contract_path=contract_path,
            receipt=updated,
            expected_owner_token=payload["owner_token"],
            marker=str(path),
        )
        if result is None or not result.ok:
            raise ContractError("; ".join((result.errors if result is not None else ["rebind failed"])))
    except (ContractError, messages.MessageError, TypeError, ValueError) as exc:
        _consume_marker(path, ok=False, reason=str(exc))
        return False
    _consume_marker(path, ok=True)
    return True


def service_rebind_markers(*, runtime_root: str | Path, limit: int = 64) -> tuple[int, int]:
    """Bounded daemon sweep across holder-local rebind markers."""
    accepted = rejected = 0
    root = Path(runtime_root) / addressing.NODES_DIRNAME
    if not root.is_dir():
        return accepted, rejected
    for path in sorted(root.glob(f"**/{REBINDS_DIRNAME}/*.json"))[:limit]:
        if service_rebind_marker(path, runtime_root=runtime_root):
            accepted += 1
        else:
            rejected += 1
    return accepted, rejected


def amendment_message_id(revision_record_ref: str, holder_address: str) -> str:
    digest = hashlib.sha256(
        f"{revision_record_ref}\0{holder_address}".encode("utf-8")
    ).hexdigest()
    return f"contract-amendment-{digest[:32]}"


def deliver_amendment_message(stale: dict, *, runtime_root: str | Path) -> bool:
    """Materialize and submit one deterministic ordinary 3a amendment message."""
    owner = stale["owner_address"]
    holder = stale["holder_address"]
    revision_ref = str(stale.get("revision_record_ref") or "")
    if not revision_ref:
        raise ContractError("current contract version has no revision record reference")
    message_id = amendment_message_id(revision_ref, holder)
    directory = addressing.messages_dir(owner, runtime_root)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{message_id}.md"
    marker = addressing.message_marker_path(owner, message_id, runtime_root)
    body = (
        "# Contract amendment\n\n"
        f"A contract you're bound to was amended: `{stale['contract_path']}`.\n\n"
        f"Re-read the canonical contract and revision record: `{revision_ref}`.\n\n"
        "This amendment wake authorizes proceeding against the re-frozen contract. "
        "Ordinary messages only persuade; they do not amend contracts.\n"
    )
    marker_payload = {
        "type": messages.MESSAGE_TYPE,
        "message_id": message_id,
        "sender": owner,
        "to": holder,
        "artifact": f"messages/{message_id}.md",
        "summary": "A bound contract was amended; re-read the canonical contract and revision.",
        "needs_answer": False,
        "tags": [AMENDMENT_TAG],
        "metadata": {
            "contract_path": stale["contract_path"],
            "prior_fingerprint": stale["held_fingerprint"],
            "new_fingerprint": stale["current_fingerprint"],
            "revision_record_ref": revision_ref,
            "amendment_authorizes_proceeding": True,
        },
        "answers_question": None,
    }
    if artifact.exists() and artifact.read_text(encoding="utf-8") != body:
        raise ContractError(f"amendment message artifact collision for {message_id}")
    if not artifact.exists():
        store.atomic_replace(artifact, lambda handle: handle.write(body))
    marker_text = json.dumps(marker_payload, indent=2, sort_keys=True) + "\n"
    if marker.exists() and marker.read_text(encoding="utf-8") != marker_text:
        raise ContractError(f"amendment message marker collision for {message_id}")
    if not marker.exists():
        store.atomic_replace(marker, lambda handle: handle.write(marker_text))
    before = (ledger.read_binding(owner) or {}).get("messages") or {}
    already = message_id in before
    messages.submit_marker(owner, marker, runtime_root=runtime_root)
    return not already


def deliver_amendment_ripple(*, runtime_root: str | Path) -> int:
    delivered = 0
    for stale in stale_receipt_holders():
        try:
            delivered += int(deliver_amendment_message(stale, runtime_root=runtime_root))
        except (ContractError, messages.MessageError, OSError, ValueError):
            continue
    return delivered
