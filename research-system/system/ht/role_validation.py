"""Semantic validation for B2 role wire objects.

The frozen B1 runtime validator remains byte-identical.  B2 callers use this
additive entry point for cross-field identities that JSON Schema cannot state
without nonstandard extensions.
"""

from __future__ import annotations

from typing import Any

from .errors import HtError
from .paths import normalize_repository_relpath
from .runtime.schema import validate as validate_runtime


_ACTION_TARGET_FIELDS = {
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
}


def _reject(message: str) -> None:
    raise HtError(f"role-wire semantic mismatch: {message} (B2 §16)")


def _payload_outputs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("plan_output", "report_output", "qa_output"):
        value = payload.get(key)
        if isinstance(value, dict):
            return [value]
    value = payload.get("artifact_outputs")
    if isinstance(value, list):
        return value
    return []


def _validate_action(value: dict[str, Any]) -> None:
    outputs = value["outputs"]
    slots = [row["slot"] for row in outputs]
    if len(slots) != len(set(slots)):
        _reject("runtime action repeats an output slot")

    payload = value["payload"]
    action_kind = value["action_kind"]
    target_field = _ACTION_TARGET_FIELDS.get(action_kind)
    if target_field is not None and value["target_ref"] != payload[target_field]:
        _reject("runtime action target differs from its payload identity")

    derived_outputs = _payload_outputs(payload)
    if derived_outputs != outputs:
        _reject("runtime action payload descriptors differ from envelope outputs")


def _validate_process_exit(value: dict[str, Any]) -> None:
    wait_status = value["runner_wait_status"]
    if value["exit_kind"] == "exited":
        if value["exit_code"] != wait_status:
            _reject("runner exit code differs from wait status")
    elif value["signal"] != -wait_status:
        _reject("runner signal differs from negative wait status")


def _validate_capture(value: dict[str, Any]) -> None:
    if value["producer_adapter_token"] == "sealed-codex-fixture/1.0.0":
        expected = f"sealed-fixture:{value['session_uuid']}"
        if value["producer_native_id"] != expected:
            _reject("sealed native identity differs from session UUID")
    final_hash = value["final_response_sha256"]
    if final_hash is not None and value["inventory"][-1]["sha256"] != final_hash:
        _reject("capture final inventory hash differs from final-response hash")


def _validate_repository_paths(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.endswith("relative_path") or key.endswith("repository_relpath"):
                if nested is not None:
                    try:
                        normalize_repository_relpath(nested)
                    except Exception as exc:
                        raise HtError(
                            f"role-wire semantic mismatch: {key} is not one normalized "
                            "repository-relative path (B2 §4)"
                        ) from exc
            _validate_repository_paths(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_repository_paths(nested)


def validate_role_wire(value: Any) -> None:
    """Validate one B2 role wire object, including cross-field identities."""

    validate_runtime("role-wire.schema.json", value)
    if not isinstance(value, dict):
        return
    _validate_repository_paths(value)
    schema_version = value.get("schema_version")
    if schema_version == "hypothesis-tree-runtime-role-action/1.0.0":
        _validate_action(value)
    elif schema_version in {
        "hypothesis-tree-runtime-role-result/1.0.0",
        "hypothesis-tree-runtime-role-terminal/1.0.0",
    }:
        if len(value["action_ids"]) != len(value["action_sha256s"]):
            _reject("action ID/hash arrays have different lengths")
    elif schema_version == "hypothesis-tree-runtime-role-process-exit/1.0.0":
        _validate_process_exit(value)
    elif schema_version == "hypothesis-tree-runtime-role-capture-manifest/1.0.0":
        _validate_capture(value)
