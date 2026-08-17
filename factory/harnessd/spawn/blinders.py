"""Derive the physical F34 local-read world for one spawned seat.

This module is deliberately policy-only.  It resolves the address-derived neighborhood, declared
launch documents, and measured runtime essentials into deterministic path facts.  ``sandbox.py``
turns those facts into SBPL; adapters apply the profile; the chokepoint records the posture.

Internet is outside this policy.  L1 god-view is derived from the canonical root address, never
from caller-supplied role text or a grantable flag.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable, Mapping

from harnessd import addressing


VERSION = "blinders-v1"
OBSERVE = "observe"
ENFORCE = "enforce"
MODES = frozenset({OBSERVE, ENFORCE})

# Empirically derived on the commissioning host (macOS 26.5.1).  These are runtime/toolchain
# roots, not user-data roots.  Missing roots are harmless policy entries and keep rendering stable
# across machines with CommandLineTools rather than full Xcode.
SYSTEM_READ_SUBPATHS = (
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/Library",
    "/dev",
    "/etc",
    "/private/etc",
    "/var/db/timezone",
    "/private/var/db/timezone",
    "/Applications/Xcode.app",
    "/Library/Developer/CommandLineTools",
    "/opt/homebrew",
)

CLAUDE_CONFIG_INNER_DENIES = ("projects", "tasks", "session-env")


class BlindersError(ValueError):
    """The readable world could not be derived without guessing."""


def _absolute(path: str | os.PathLike[str], *, label: str) -> str:
    value = os.path.abspath(os.fspath(path))
    if not os.path.isabs(value):
        raise BlindersError(f"{label} must resolve to an absolute path: {path!r}")
    return os.path.normpath(value)


def _declared_path(
    value: object,
    *,
    base_dir: str,
    label: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    raw = str(value).strip()
    if not raw or raw.startswith("("):
        return ()
    relative = not os.path.isabs(raw)
    path = os.path.join(base_dir, raw) if relative else raw
    logical = _absolute(path, label=label)
    if relative:
        base = _absolute(base_dir, label=f"{label} base")
        if os.path.commonpath((base, logical)) != base:
            raise BlindersError(f"{label} escapes its declared base: {raw!r}")
    resolved = os.path.realpath(logical)
    return (logical,) if resolved == logical else (logical, resolved)


def _visible_reference_targets(reference_map_json_file: str | None) -> tuple[str, ...]:
    if not reference_map_json_file:
        return ()
    path = Path(reference_map_json_file)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BlindersError(f"reference map is unreadable or malformed: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BlindersError(f"reference map must contain an object: {path}")
    references = payload.get("references", [])
    if not isinstance(references, list):
        raise BlindersError(f"reference map references must be a list: {path}")
    targets: list[str] = []
    for index, block in enumerate(references):
        if not isinstance(block, dict):
            raise BlindersError(f"reference map references[{index}] must be an object")
        resolved_paths = block.get("resolved_paths", [])
        if not isinstance(resolved_paths, list):
            raise BlindersError(
                f"reference map references[{index}].resolved_paths must be a list"
            )
        for path_index, row in enumerate(resolved_paths):
            if not isinstance(row, dict) or not row.get("absolute_path"):
                raise BlindersError(
                    "reference map resolved target must name absolute_path: "
                    f"references[{index}].resolved_paths[{path_index}]"
                )
            target = str(row["absolute_path"])
            if not os.path.isabs(target):
                raise BlindersError(f"reference target is not absolute: {target!r}")
            logical = os.path.normpath(target)
            targets.append(logical)
            resolved = os.path.realpath(logical)
            if resolved != logical:
                targets.append(resolved)
    return tuple(targets)


def _parent_address(
    node_address: str,
    bindings: Mapping[str, Mapping],
) -> str | None:
    binding = bindings.get(node_address)
    if binding is not None and binding.get("parent_address"):
        return str(binding["parent_address"])
    path = addressing.node_path(node_address).strip("/")
    if "/" not in path:
        return None
    return f"{path.rsplit('/', 1)[0]}#exec"


def _neighborhood(
    node_address: str,
    runtime_root: str,
    bindings: Mapping[str, Mapping],
) -> tuple[str | None, tuple[str, ...]]:
    parent = _parent_address(node_address, bindings)
    if not parent:
        return None, ()
    parent_dir = str(addressing.node_dir(parent, runtime_root))
    own_path = addressing.node_path(node_address)
    sibling_dirs: set[str] = set()
    for address, binding in bindings.items():
        if str(binding.get("parent_address") or "") != parent:
            continue
        if addressing.node_path(address) == own_path:
            continue
        sibling_dirs.add(str(addressing.node_dir(address, runtime_root)))
    return parent_dir, tuple(sorted(sibling_dirs))


def _runtime_package_roots(runtime: str, harness_root: str) -> tuple[str, ...]:
    if runtime == "claude-code":
        return (
            os.path.join(harness_root, ".cc-pinned"),
            os.path.join(harness_root, "harnessd"),
        )
    if runtime == "codex":
        return (
            os.path.join(harness_root, ".codex-pinned", "node_modules"),
            os.path.join(harness_root, "harnessd"),
        )
    raise BlindersError(f"unsupported runtime for blinders: {runtime!r}")


def _dedupe(values: Iterable[str]) -> list[str]:
    return sorted({os.path.normpath(str(value)) for value in values if str(value).strip()})


def _runtime_socket_paths(runtime_root: str) -> list[str]:
    logical = os.path.join(runtime_root, ".harnessd", "harnessd.sock")
    return _dedupe([logical, os.path.realpath(logical)])


def derive_policy(
    *,
    node_address: str,
    runtime_root: str,
    bindings: Mapping[str, Mapping],
    mode: str,
    workspace: str,
    load_manifest: Iterable[str],
    runtime: str,
    harness_root: str,
    system_prompt_file: str | None = None,
    spec_pointer: str | None = None,
    frozen_acceptance_ref: str | None = None,
    launch_packet_file: str | None = None,
    reference_map_file: str | None = None,
    reference_map_json_file: str | None = None,
    git_dir: str | None = None,
    git_common_dir: str | None = None,
    role_variant: str | None = None,
) -> dict:
    """Return a deterministic JSON-safe physical read policy.

    ``role_variant`` is accepted only so callers/tests can prove it cannot grant god-view.  It is
    intentionally unused.
    """
    del role_variant
    if mode not in MODES:
        raise BlindersError(f"unknown blinders mode {mode!r}; expected observe|enforce")
    runtime_root = _absolute(runtime_root, label="runtime_root")
    workspace = _absolute(workspace, label="workspace")
    harness_root = _absolute(harness_root, label="harness_root")
    node_path = addressing.node_path(node_address).strip("/")
    l1_god_view = node_path == "L1"
    own = str(addressing.node_dir(node_address, runtime_root))
    parent, siblings = _neighborhood(node_address, runtime_root, bindings)

    declared: list[str] = []
    # Neutral load-manifest/system-prompt paths are harness-root-relative.  Node contract and
    # materialized launch-surface pointers are workspace-relative when not already absolute.
    for index, value in enumerate([*list(load_manifest), system_prompt_file]):
        declared.extend(
            _declared_path(
                value,
                base_dir=harness_root,
                label=f"harness document {index}",
            )
        )
    declared_inputs = [
        spec_pointer,
        frozen_acceptance_ref,
        launch_packet_file,
        reference_map_file,
        reference_map_json_file,
    ]
    for index, value in enumerate(declared_inputs):
        declared.extend(
            _declared_path(
                value,
                base_dir=workspace,
                label=f"declared document {index}",
            )
        )
    declared.extend(_visible_reference_targets(reference_map_json_file))

    allow_subpaths = [
        own,
        *SYSTEM_READ_SUBPATHS,
        *_runtime_package_roots(runtime, harness_root),
    ]
    # Python hook callbacks run through the daemon interpreter.  Usually this is inside
    # /opt/homebrew, but retaining the exact executable/prefix makes the dependency explicit.
    allow_literals = ["/", *declared, sys.executable]
    if git_dir:
        allow_subpaths.append(_absolute(git_dir, label="git_dir"))
    if git_common_dir:
        allow_subpaths.append(_absolute(git_common_dir, label="git_common_dir"))

    direct_surfaces: list[str] = []
    if parent:
        direct_surfaces.append(parent)
    direct_surfaces.extend(siblings)

    return {
        "version": VERSION,
        "mode": mode,
        "node_address": node_address,
        "l1_god_view": l1_god_view,
        "allow_subpaths": _dedupe(allow_subpaths),
        "allow_literals": _dedupe(allow_literals),
        "direct_surfaces": _dedupe(direct_surfaces),
        "declared_documents": _dedupe(declared),
        "runtime": runtime,
        "runtime_state_roots": [],
        "runtime_inner_denies": [],
        "runtime_inner_allows": [],
        "runtime_control_sockets": _runtime_socket_paths(runtime_root),
    }


def derive_exact_policy(
    *,
    node_address: str,
    runtime_root: str,
    workspace: str,
    exact_documents: Iterable[str],
    runtime: str,
    harness_root: str,
    runtime_documents: Iterable[str] = (),
) -> dict:
    """Return the fail-closed physical policy for a semantic blind-window seat.

    This is a deliberate exception to the deployment-wide observe-first commissioning posture:
    blindness is the check, so a semantic seat that cannot start under enforce mode must not open.
    The ordinary parent/sibling/reference-map neighborhood is absent by construction.  The seat
    receives its isolated workroot, measured runtime/toolchain roots, and exact harness-notary
    inputs only.
    """

    runtime_root = _absolute(runtime_root, label="runtime_root")
    workspace = _absolute(workspace, label="workspace")
    harness_root = _absolute(harness_root, label="harness_root")
    own = _absolute(workspace, label="semantic workspace")
    declared: list[str] = []
    for index, value in enumerate([*list(exact_documents), *list(runtime_documents)]):
        declared.extend(
            _declared_path(
                value,
                base_dir=own,
                label=f"semantic exact document {index}",
            )
        )
    return {
        "version": f"{VERSION}-exact-v1",
        "mode": ENFORCE,
        "node_address": node_address,
        "l1_god_view": False,
        "exact_declared": True,
        "allow_subpaths": _dedupe(
            [
                own,
                *SYSTEM_READ_SUBPATHS,
                *_runtime_package_roots(runtime, harness_root),
            ]
        ),
        "allow_literals": _dedupe(["/", *declared, sys.executable]),
        "direct_surfaces": [],
        "declared_documents": _dedupe(declared),
        "runtime": runtime,
        "runtime_state_roots": [],
        "runtime_inner_denies": [],
        "runtime_inner_allows": [],
        "runtime_control_sockets": _runtime_socket_paths(runtime_root),
    }


def with_runtime_state(
    policy: Mapping,
    *,
    runtime: str,
    state_root: str,
    session_uuid: str | None = None,
) -> dict:
    """Return ``policy`` finalized for the exact runtime state home.

    Codex calls this only after minting the per-seat CODEX_HOME. Claude passes its pinned config
    root and receives the proven cross-seat transcript/task carve-outs. Once Claude has minted its
    session UUID, its exact ``session-env/<uuid>`` directory is reopened after the shared
    ``session-env`` deny.
    """
    if runtime not in {"claude-code", "codex"}:
        raise BlindersError(f"unsupported runtime state {runtime!r}")
    root = _absolute(state_root, label=f"{runtime} state_root")
    result = dict(policy)
    result["runtime"] = runtime
    result["runtime_state_roots"] = _dedupe(
        [*list(policy.get("runtime_state_roots") or []), root]
    )
    inner = list(policy.get("runtime_inner_denies") or [])
    inner_allows = list(policy.get("runtime_inner_allows") or [])
    if runtime == "claude-code":
        inner.extend(os.path.join(root, rel) for rel in CLAUDE_CONFIG_INNER_DENIES)
        if session_uuid is not None:
            session = str(session_uuid).strip()
            if (
                not session
                or os.path.basename(session) != session
                or session in {".", ".."}
            ):
                raise BlindersError(f"invalid Claude session UUID path segment: {session_uuid!r}")
            inner_allows.append(os.path.join(root, "session-env", session))
    result["runtime_inner_denies"] = _dedupe(inner)
    result["runtime_inner_allows"] = _dedupe(inner_allows)
    return result
