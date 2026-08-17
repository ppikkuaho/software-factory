"""Schema loading and validation (D7 gate, first pipeline step).

All schemas in system/schemas/ are loaded into a referencing.Registry keyed by
$id so node.schema.json's cross-file $refs (claim/measurement) resolve. Doc types
map to schema filenames.
"""

from __future__ import annotations

import functools
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from . import jsonio
from .errors import HtError


_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _real_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None

# doc_type -> schema filename
DOC_SCHEMA = {
    "node": "node.schema.json",
    "dispatch": "dispatch.schema.json",
    "claim": "claim.schema.json",
    "measurement": "measurement.schema.json",
    "tree": "tree.schema.json",
    "ledger_entry": "ledger_entry.schema.json",
    "ledger_union_index": "ledger_union_index.schema.json",
    "index": "index.schema.json",
    "instruments_registry": "instruments_registry.schema.json",
    "phase": "phase.schema.json",
    "pc_decision": "pc_decision.schema.json",
    "issue": "issue.schema.json",
    "ratification_item": "ratification_item.schema.json",
    "merge_record": "merge_record.schema.json",
    "gate_review": "gate_review.schema.json",
    "issue_queue": "issue_queue.schema.json",
    "interrupt": "interrupt.schema.json",
    "composed_tree": "composed_tree.schema.json",
    "subgoal": "subgoal.schema.json",
    "task_package": "task-package.schema.json",
    "inbox_delivery": "inbox-delivery.schema.json",
    "inbox_receipt": "inbox-receipt.schema.json",
    "action_receipt": "action-receipt.schema.json",
    "producer_return": "producer-return.schema.json",
    "wave_b2_leak_closures": "wave-b2-leak-closures.schema.json",
    "wave_b2_commissioning_result": "wave-b2-commissioning-result.schema.json",
    "wave_b2_commissioning_manifest": "wave-b2-commissioning-manifest.schema.json",
}


@functools.lru_cache(maxsize=None)
def _load_dir(schemas_dir_str: str) -> tuple[Registry, dict[str, dict]]:
    schemas_dir = Path(schemas_dir_str)
    resources: list[tuple[str, Resource]] = []
    by_name: dict[str, dict] = {}
    for f in sorted(schemas_dir.glob("*.json")):
        schema = jsonio.load(f)
        by_name[f.name] = schema
        sid = schema.get("$id")
        if sid:
            resources.append((sid, Resource.from_contents(schema)))
    registry = Registry().with_resources(resources)
    return registry, by_name


def validate(schemas_dir: Path, doc_type: str, doc: Any) -> None:
    """Schema-validate `doc`; raise HtError naming the first failure."""
    if doc_type not in DOC_SCHEMA:
        raise HtError(f"no schema registered for doc type '{doc_type}' (D7)")
    registry, by_name = _load_dir(str(schemas_dir))
    schema = by_name[DOC_SCHEMA[doc_type]]
    validator = Draft202012Validator(
        schema,
        registry=registry,
        format_checker=_FORMAT_CHECKER,
    )
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        loc = "/".join(str(p) for p in e.path) or "<root>"
        raise HtError(
            f"schema-nonconforming {doc_type} at '{loc}': {e.message} "
            f"(B4 §9 schema conformance)"
        )
    _validate_b2_semantics(doc_type, doc)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _validate_unique_slots(rows: list[dict], label: str) -> None:
    slots = [row.get("slot") for row in rows]
    if len(slots) != len(set(slots)):
        raise HtError(f"{label} repeats a slot identity (B2 §9.8)")


def _validate_b2_semantics(doc_type: str, doc: Any) -> None:
    """Relationships JSON Schema cannot express without caller-selected state."""

    if not isinstance(doc, dict):
        return
    if doc_type == "task_package":
        contract = doc["output_contract"]
        _validate_unique_slots(contract["slots"], "task-package output contract")
        action_kinds = [row["action_kind"] for row in contract["actions"]]
        if len(action_kinds) != len(set(action_kinds)):
            raise HtError("task-package output contract repeats an action kind (B2 §8)")
        for row in contract["actions"]:
            overlap = set(row["required_output_slots"]) & set(row["optional_output_slots"])
            if overlap:
                raise HtError(
                    f"task-package action slot is both required and optional: {sorted(overlap)} (B2 §8)"
                )
        context_refs = doc["semantic_brief"]["context_refs"]
        reference_refs = [row["ref"] for row in doc["reference_map"]]
        if context_refs != reference_refs:
            raise HtError("task-package context refs do not equal reference-map order (B2 §9)")
        kind = doc["package_kind"]
        expected_target = (
            "pc#principal-coordinator"
            if kind == "pc-wake"
            else doc["subgoal_ref"]
            if kind == "director-inbox"
            else doc["dispatch_ref"]
        )
        if doc["target_ref"] != expected_target:
            raise HtError("task-package target does not equal its applicable identity (B2 §9)")
        declared_slots = [row["slot"] for row in contract["slots"]]
        action_slots = [
            slot
            for action in contract["actions"]
            for slot in action["required_output_slots"] + action["optional_output_slots"]
        ]
        if declared_slots != action_slots:
            raise HtError("task-package slot declarations disagree with action order (B2 §9.8)")
    elif doc_type == "action_receipt":
        payload = doc["payload"]
        target_field = {
            "pc.route-subgoal": "issue_ref",
            "pc.annotate-ratification": "item_ref",
            "director.create-dispatch": "node_ref",
            "director.raise-interrupt": "subgoal_ref",
            "director.recall-dispatch": "dispatch_ref",
            "senior.submit-plan": "dispatch_ref",
            "senior.submit-report": "dispatch_ref",
            "junior.return-unit-artifact": "dispatch_ref",
            "checker.return-qa": "dispatch_ref",
            "verifier.adjudicate-report": "dispatch_ref",
        }.get(doc["action_kind"])
        if target_field is not None and doc["target_ref"] != payload[target_field]:
            raise HtError("action-receipt target differs from payload identity (B2 §12)")
        descriptors: list[dict] = []
        for key in ("plan_output", "report_output", "qa_output"):
            value = payload.get(key)
            if isinstance(value, dict):
                descriptors.append(value)
        values = payload.get("artifact_outputs")
        if isinstance(values, list):
            descriptors.extend(row for row in values if isinstance(row, dict))
        _validate_unique_slots(descriptors, "action-receipt payload")
    elif doc_type == "wave_b2_commissioning_manifest":
        entries = doc["entries"]
        paths = [row["relative_path"] for row in entries]
        if paths != sorted(paths, key=lambda value: value.encode("utf-8")):
            raise HtError("commissioning manifest entries are not UTF-8 lexical (B2 §23)")
        if len(paths) != len(set(paths)):
            raise HtError("commissioning manifest repeats a relative path (B2 §23)")
        if any(
            not row["relative_path"].startswith("nested-root/")
            and row["mode"] != "0444"
            for row in entries
        ):
            raise HtError("commissioning evidence file has wrong frozen mode (B2 §23)")
        digest = hashlib.sha256(_canonical_json_bytes(entries)).hexdigest()
        if doc["entries_sha256"] != digest:
            raise HtError("commissioning manifest entries_sha256 mismatch (B2 §23)")
        result_rows = [row for row in entries if row["relative_path"] == "result.json"]
        if len(result_rows) != 1 or result_rows[0]["sha256"] != doc["result_sha256"]:
            raise HtError("commissioning manifest result_sha256 is not its result row (B2 §23)")
