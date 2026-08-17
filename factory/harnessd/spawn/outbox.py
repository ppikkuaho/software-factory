"""The spawn-request OUTBOX — the agent-facing half of the live cascade (FORK-SPAWN-CHANNEL).

WHY a request mailbox instead of a direct control-socket call:
  A jailed agent (sandbox-exec write-jail) can write only its OWN workroot. If we gave it a socket to
  the daemon's IPC, it would reach the WHOLE control plane (spawn/kill/transition/reconcile on ANY
  node) — a privilege escalation AND a single-writer violation (the agent would mutate state the daemon
  is meant to own). So the agent never talks to the control plane. It DROPS a spawn-REQUEST file into a
  ``.harness-outbox/`` dir inside its workroot (a write the jail already allows), and the privileged
  daemon adjudicates it. This mirrors the system's whole spine: request -> single writer adjudicates ->
  apply (the WAL ledger, the F-024 chokepoint, the parent-fence).

THE THREE GUARANTEES (all enforced DAEMON-side; the agent is untrusted):
  1. NAMESPACE CONFINEMENT — the agent supplies only a child LEAF-NAME (``child_name``), never a full
     address. The daemon composes the child address FROM the parent's own address, so a child can ONLY
     land inside the requesting node's subtree. A name carrying ``/``, ``..``, ``#``, or whitespace is
     refused (it can't be a leaf-name) — re-validated daemon-side even if a hand-written file bypassed
     the client check.
  2. PROVENANCE — the daemon services node X's outbox under X's OWN live owner_token, passed as
     ``expected_parent_owner_token`` to register_and_spawn_child (the parent-fence). A request in X's
     outbox can ONLY spawn under X; it can't forge a spawn under a sibling.
  3. REJECT-WITH-REASON, NEVER SILENT-DROP — a malformed/invalid/failed request is renamed ``.rejected``
     (with a ``.reason`` sibling the agent reads in its own workroot). The flow never silently skips a
     request (that would be exactly the "leak" the behavioural-validation pass guards against).

IDEMPOTENT: a successfully-serviced request is renamed ``.done`` and is not re-processed; even if a
crash lands between spawn and rename, a re-service calls register_and_spawn_child again, which is
single-owner-safe (a live child is not re-opened — the planned-expected claim loses). At-least-once
servicing + idempotent spawn = exactly-once effect.

LEAF RULE: L5 is the executor leaf — it spawns no children, so ``service_all_outboxes`` skips L5 nodes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.contracts as contracts
import harnessd.ledger as ledger
import harnessd.notary as notary
import harnessd.spawn.chokepoint as chokepoint
import harnessd.states as states

# The outbox dir name, relative to a node's workroot (jail-writable). One JSON file per request.
OUTBOX_DIRNAME: str = ".harness-outbox"

# A child leaf-name: lowercase alnum start, then alnum / dot / underscore / hyphen. NO '/', '..', '#',
# or whitespace — so it can never escape the parent's namespace or inject a role suffix. Capped length.
# \A…\Z (NOT ^…$): in Python ``$`` also matches BEFORE a trailing newline, so ``^…$`` would let
# ``"parser\n"`` through and embed a newline in the composed address / tmux session name. \Z anchors the
# absolute end and closes that hole (the char class already excludes interior whitespace).
_CHILD_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")

# A brief over this size is refused (a request file is a control message, not a payload channel).
MAX_BRIEF_BYTES: int = 64 * 1024

# The executor leaf level — these nodes spawn no children (service_all_outboxes skips them).
LEAF_LEVEL: str = "L5"

# A child is NEVER an L1 root (L1 is genesis-only / parentless), so L1 is not a spawnable child level.
SPAWNABLE_LEVELS: tuple = ("L2", "L3", "L4", "L5", "L5+")  # E4: L5+ = the M52 reviewer seat

# Level depth order (L1 shallowest … L5/L5+ deepest). A child must be STRICTLY deeper than its parent —
# an L2 spawns L3s, never another L2 or an L1. This blocks both same-level and up-level spawns.
# L5+ (the per-unit reviewer, QUALITY-GATE M52) sits BELOW L5 in depth so both an L4 (spawning the
# review seat alongside its executors) and a future L5-finish harness verb descend into it legally.
_LEVEL_ORDER: dict = {"L1": 1, "L2": 2, "L3": 3, "L4": 4, "L5": 5, "L5+": 6}

# Runaway / DoS backstops. Real fan-out is bounded by the decomposition (a parent has a handful of
# children); these are safety caps, not design limits. The per-sweep cap keeps one tick from spawning
# thousands of children (blocking the reconcile loop); the per-parent cap bounds total live children.
MAX_REQUESTS_PER_SWEEP: int = 16   # requests drained per node per tick (the remainder drain next tick)
MAX_CHILDREN_PER_PARENT: int = 64  # live children a single parent may hold (admission backstop)

PURPOSE_TEST_AUTHOR: str = "test_author"
PURPOSE_PLANNING: str = "planning"
_ALLOWED_PURPOSES: tuple[str, ...] = (PURPOSE_TEST_AUTHOR, PURPOSE_PLANNING)
_PENDING_TEST_REFRESH_STATES: tuple[str, ...] = ("pending_l5_review", "pending_l3_approval")


@dataclass
class ServiceOutcome:
    """One serviced request: spawned, queued for admission, or rejected (with a reason)."""

    request_path: str
    status: str  # "spawned" | "queued" | "rejected"
    child_address: Optional[str] = None
    reason: Optional[str] = None
    # True when the request resolved to a child that ALREADY exists live (idempotent re-service) —
    # status stays "spawned" for the agent, but the sweep must NOT count it against the in-sweep
    # fan-out cap (the child is already in the _live_child_count base).
    already_live: bool = False


# --------------------------------------------------------------------------- #
# validation (the explicit contract — P1: the daemon owns the criteria)
# --------------------------------------------------------------------------- #

def validate_request(obj: object) -> tuple[bool, str]:
    """Validate a parsed request object against the explicit contract. Returns (ok, reason).

    The agent fills only the parts it legitimately owns: a child leaf-NAME, the child LEVEL, and the
    child's BRIEF (its task). It never supplies the full address (composed daemon-side) or a token
    (the daemon presents the parent's). Every field is checked here so a hand-written file can't slip
    an unsafe name past the daemon.
    """
    if not isinstance(obj, dict):
        return False, "request is not a JSON object"
    name = obj.get("child_name")
    if not isinstance(name, str) or not _CHILD_NAME_RE.match(name) or ".." in name:
        return False, f"child_name {name!r} is not a safe leaf-name (no '/', '..', '#', or whitespace)"
    level = obj.get("child_level")
    if not isinstance(level, str) or level not in SPAWNABLE_LEVELS:
        return False, (f"child_level {level!r} is not a spawnable child level {list(SPAWNABLE_LEVELS)} "
                       "(a child is never an L1 root)")
    # The brief is OPTIONAL (FORK-BRIEF-DERIVATION): the DEFAULT is to pre-author brief.md + acceptance.md
    # INTO the child node and let the spawn DERIVE the pointers; an inline brief is the OVERRIDE/exception.
    # Absent/None -> derivation default. Present -> must be a non-empty string within the size cap.
    brief = obj.get("brief")
    if brief is not None:
        if not isinstance(brief, str) or not brief.strip():
            return False, "brief, when provided, must be a non-empty string (omit it to derive from the pre-authored node)"
        if len(brief.encode("utf-8")) > MAX_BRIEF_BYTES:
            return False, f"brief exceeds {MAX_BRIEF_BYTES} bytes"
    purpose = obj.get("purpose")
    if purpose is not None:
        if not isinstance(purpose, str) or purpose not in _ALLOWED_PURPOSES:
            return False, f"purpose {purpose!r} is not one of {list(_ALLOWED_PURPOSES)}"
    test_refresh = obj.get("test_refresh")
    if test_refresh is not None and not isinstance(test_refresh, bool):
        return False, "test_refresh, when provided, must be a boolean"
    if test_refresh is True and purpose != PURPOSE_TEST_AUTHOR:
        return False, "test_refresh=true requires purpose='test_author'"
    test_refresh_for = obj.get("test_refresh_for")
    if test_refresh_for is not None:
        if not isinstance(test_refresh_for, str) or not test_refresh_for.strip():
            return False, "test_refresh_for, when provided, must be a non-empty string"
        if len(test_refresh_for.encode("utf-8")) > 4096:
            return False, "test_refresh_for exceeds 4096 bytes"
    accepted_test_package = obj.get("accepted_test_package")
    if accepted_test_package is not None:
        if not isinstance(accepted_test_package, str) or not accepted_test_package.strip():
            return False, "accepted_test_package, when provided, must be a non-empty string"
        if len(accepted_test_package.encode("utf-8")) > 4096:
            return False, "accepted_test_package exceeds 4096 bytes"
    no_tests_ref = obj.get("no_executable_tests_exception_ref")
    if accepted_test_package is not None and no_tests_ref is not None:
        return False, "accepted_test_package and no_executable_tests_exception_ref are mutually exclusive"
    if no_tests_ref is not None:
        if not isinstance(no_tests_ref, str) or not no_tests_ref.strip():
            return False, "no_executable_tests_exception_ref, when provided, must be a non-empty string"
        if len(no_tests_ref.encode("utf-8")) > 4096:
            return False, "no_executable_tests_exception_ref exceeds 4096 bytes"
        ref_path = Path(no_tests_ref)
        if ref_path.is_absolute() or ".." in ref_path.parts:
            return False, "no_executable_tests_exception_ref must be a relative path inside the parent node"
    return True, ""


def compose_child_address(parent_address: str, child_name: str) -> str:
    """Compose the child address FROM the parent's address (namespace confinement lives here).

    ``proj/widget#exec`` + ``parser`` -> ``proj/widget/parser#exec``. The child inherits the parent's
    role suffix (the working ``#exec`` incarnation). Because the child path is the parent path plus a
    validated leaf-name, the child ALWAYS lands inside the parent's subtree — the agent cannot name a
    node elsewhere in the tree.
    """
    path, sep, role = parent_address.rpartition("#")
    if not sep:  # no role suffix on the parent — treat the whole thing as the path, default role
        path, role = parent_address, "exec"
    return f"{path}/{child_name}#{role or 'exec'}"


# --------------------------------------------------------------------------- #
# agent-side writer (runs inside the jail; writes ONLY the node's own workroot)
# --------------------------------------------------------------------------- #

def request_child_spawn(
    workroot,
    *,
    child_name: str,
    child_level: str,
    brief: Optional[str] = None,
    purpose: Optional[str] = None,
    test_refresh: Optional[bool] = None,
    test_refresh_for: Optional[str] = None,
    accepted_test_package: Optional[str] = None,
    no_executable_tests_exception_ref: Optional[str] = None,
) -> Path:
    """Drop a spawn-request into the node's own outbox (the agent-side call; jail-writable workroot).

    The DEFAULT is derivation: pre-author ``<child>/brief.md`` + ``<child>/acceptance.md`` into the
    child node (a subdir of your own workspace, writable under the nested subtree-jail) FIRST, then call
    this with NO ``brief`` — the spawn derives the pointers from those files. Pass ``brief`` only as the
    OVERRIDE/exception (a throwaway task you didn't pre-author a node file for).

    Client-side fail-fast: the same contract the daemon enforces is checked here too. (The DAEMON
    re-validates regardless — this check is convenience, not the trust boundary.)
    """
    request: dict = {"child_name": child_name, "child_level": child_level}
    if brief is not None:
        request["brief"] = brief
    if purpose is not None:
        request["purpose"] = purpose
    if test_refresh is not None:
        request["test_refresh"] = test_refresh
    if test_refresh_for is not None:
        request["test_refresh_for"] = test_refresh_for
    if accepted_test_package is not None:
        request["accepted_test_package"] = accepted_test_package
    if no_executable_tests_exception_ref is not None:
        request["no_executable_tests_exception_ref"] = no_executable_tests_exception_ref
    ok, reason = validate_request(request)
    if not ok:
        raise ValueError(f"invalid spawn request: {reason}")
    outbox_dir = Path(workroot) / OUTBOX_DIRNAME
    outbox_dir.mkdir(parents=True, exist_ok=True)
    seq = _next_seq(outbox_dir)
    path = outbox_dir / f"{seq:04d}-{child_name}.json"
    path.write_text(json.dumps(request, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _request_spawn_metadata(obj: dict) -> dict:
    """Daemon-side metadata carried into the child binding for process-control semantics."""
    metadata: dict = {}
    if obj.get("purpose") == PURPOSE_TEST_AUTHOR:
        metadata["child_purpose"] = PURPOSE_TEST_AUTHOR
    if obj.get("purpose") == PURPOSE_PLANNING:
        metadata["child_purpose"] = PURPOSE_PLANNING
    if obj.get("test_refresh") is True:
        metadata["test_refresh"] = True
    if obj.get("test_refresh_for") is not None:
        metadata["test_refresh_for"] = obj.get("test_refresh_for")
    if obj.get("accepted_test_package") is not None:
        metadata["accepted_test_package"] = obj.get("accepted_test_package")
    if obj.get("no_executable_tests_exception_ref") is not None:
        metadata["no_executable_tests_exception_ref"] = obj.get("no_executable_tests_exception_ref")
    return metadata


def _reject_l4_l5_spawn_for_pending_test_refresh(
    *,
    node_address: str,
    parent_level: Optional[str],
    child_level: str,
    child_metadata: dict,
) -> Optional[str]:
    """Return a visible refusal reason when L4 is waiting on refreshed-acceptance approval."""
    if parent_level != "L4" or child_level != "L5":
        return None
    if (
        child_metadata.get("child_purpose") == PURPOSE_TEST_AUTHOR
        and child_metadata.get("test_refresh") is not True
    ):
        return None
    parent = ledger.read_binding(node_address) or {}
    state = parent.get("test_refresh_state")
    if state in _PENDING_TEST_REFRESH_STATES:
        if (
            child_metadata.get("child_purpose") == PURPOSE_TEST_AUTHOR
            and child_metadata.get("test_refresh") is True
        ):
            return (
                f"parent {node_address!r} already has test_refresh_state={state!r}; "
                "finish, approve, or abandon the existing refreshed-acceptance request before "
                "starting another one"
            )
        return (
            f"parent {node_address!r} has test_refresh_state={state!r}; implementation L5 spawns "
            "must wait until the refreshed acceptance has passed L5+ review and L3 approval"
        )
    return None


def _next_seq(outbox_dir: Path) -> int:
    """Monotonic sequence from the existing files (no wall-clock — deterministic, resume-safe)."""
    mx = 0
    for p in outbox_dir.iterdir():
        head = p.name.split("-", 1)[0]
        if head.isdigit():
            mx = max(mx, int(head))
    return mx + 1


# --------------------------------------------------------------------------- #
# daemon-side intake (the privileged adjudicator — the REAL spawn happens here)
# --------------------------------------------------------------------------- #

def _pending_requests(outbox_dir: Path) -> list[Path]:
    """Pending request files: ``*.json`` not yet consumed (.done / .rejected are terminal).

    Capped at ``MAX_REQUESTS_PER_SWEEP`` per tick: the remainder stay pending and drain on later ticks
    (still visible, never dropped) — a rate limit, not a silent truncation, so a flood of requests can't
    block one reconcile tick spawning thousands of children at once.
    """
    if not outbox_dir.is_dir():
        return []
    pending = sorted(p for p in outbox_dir.iterdir() if p.is_file() and p.suffix == ".json")
    return pending[:MAX_REQUESTS_PER_SWEEP]


def _live_child_count(parent_address: str) -> int:
    """Count the parent's currently-live (non-terminal) children — the fan-out admission base."""
    return sum(1 for b in ledger.all_nodes().values()
               if b.get("parent_address") == parent_address and not states.is_terminal(b.get("state")))


def _child_already_live(child_address: str) -> bool:
    """True when a binding ALREADY exists live (non-terminal) at the composed child address — the
    idempotent re-service predicate, shared by the pre-cap check AND the post-spawn backstop in
    ``_service_one`` so the two cannot drift apart."""
    existing = ledger.read_binding(child_address)
    return existing is not None and not states.is_terminal(existing.get("state"))


def _unresolved_same_address_gate(existing: Optional[dict]) -> Optional[str]:
    """Return a visible refusal reason when a live child is waiting on parent gate resolution."""
    if not existing or states.is_terminal(existing.get("state")):
        return None
    if existing.get("gate_state") == "gate_escalated":
        return (
            f"child {existing.get('node_address')!r} is already live with gate_state='gate_escalated'; "
            "resolve the gate escalation before reusing the same child address"
        )
    return None


def _file_stamp(path: Path) -> dict:
    return notary.stamp(path)


def _stale_planning_phase_reuse(existing: Optional[dict], child_address: str, obj: dict) -> Optional[str]:
    """Refuse a fresh execution-L3 that would boot from the previous planning-L3 task files.

    L2 may intentionally re-open the same area address after plan-alignment PASS, but the fresh
    execution incarnation must inherit canonical execution inputs. The daemon cannot silently treat
    ``exec-brief.md`` aliases as startup material while ``brief.md`` / ``acceptance.md`` still carry
    the planning phase. The parent either refreshes the canonical files or uses a distinct child name.
    """
    if not existing or not states.is_terminal(existing.get("state")):
        return None
    if existing.get("child_purpose") != PURPOSE_PLANNING:
        return None
    if obj.get("purpose") == PURPOSE_PLANNING:
        return None
    if ledger.RUNTIME_ROOT is None:
        return (
            f"child {child_address!r} previously completed as a planning-L3; same-address execution "
            "reuse cannot verify refreshed canonical brief.md/acceptance.md without a runtime root"
        )

    prior_stamps = existing.get("frozen_input_stamps")
    if not isinstance(prior_stamps, dict) or not prior_stamps:
        return (
            f"child {child_address!r} previously completed as a planning-L3; same-address execution "
            "reuse requires refreshed canonical brief.md and acceptance.md, but the prior input "
            "stamps are unavailable. Use a distinct child_name or refresh the canonical files and retry."
        )

    node_dir = addressing.node_dir(child_address, ledger.RUNTIME_ROOT)
    stale: list[str] = []

    # An inline brief override will rewrite brief.md during spawn, so the current on-disk brief need
    # not already differ in that exception path. Acceptance still has no request-payload override.
    if obj.get("brief") is None:
        previous = prior_stamps.get("brief.md")
        current = _file_stamp(node_dir / "brief.md")
        if not current.get("present"):
            stale.append("brief.md missing")
        elif isinstance(previous, dict) and current == previous:
            stale.append("brief.md unchanged from planning phase")
        elif not isinstance(previous, dict):
            stale.append("brief.md prior stamp unavailable")

    previous_acceptance = prior_stamps.get("acceptance.md")
    current_acceptance = _file_stamp(node_dir / "acceptance.md")
    if not current_acceptance.get("present"):
        stale.append("acceptance.md missing")
    elif isinstance(previous_acceptance, dict) and current_acceptance == previous_acceptance:
        stale.append("acceptance.md unchanged from planning phase")
    elif not isinstance(previous_acceptance, dict):
        stale.append("acceptance.md prior stamp unavailable")

    if not stale:
        return None
    return (
        f"child {child_address!r} previously completed as a planning-L3; execution reuse at the "
        "same address requires refreshed canonical brief.md and acceptance.md before spawn "
        f"({'; '.join(stale)}). Write the execution brief/rubric to those canonical files, or use a "
        "distinct child_name for the execution-L3. Files such as exec-brief.md are optional notes and "
        "are not the startup task package."
    )


def _consume(path: Path, status: str, reason: str = "") -> None:
    """Rename a serviced request to its terminal name (.done / .rejected) so it is never re-processed.

    For a rejection, also drop a ``<name>.reason`` sibling the agent can read in its own workroot —
    surfacing WHY, never a silent skip.
    """
    terminal = path.with_name(path.name + (".done" if status == "spawned" else ".rejected"))
    path.replace(terminal)
    if status == "rejected" and reason:
        terminal.with_name(terminal.name + ".reason").write_text(reason + "\n", encoding="utf-8")


def service_outbox(node_address: str) -> list[ServiceOutcome]:
    """Service every pending request in ONE node's outbox under THAT node's identity (the daemon entry).

    For each pending request: parse -> validate -> compose the child address from THIS node's address
    -> register_and_spawn_child(parent=node, child, expected_parent_owner_token=node's live token). A
    spawn renames the request .done; any failure renames it .rejected with a reason. A missing/terminal
    node, or a node with no outbox, services nothing.
    """
    parent = ledger.read_binding(node_address)
    if parent is None or states.is_terminal(parent.get("state")):
        return []
    workspace = parent.get("workspace")
    if not workspace:
        return []
    outbox_dir = Path(workspace) / OUTBOX_DIRNAME
    parent_token = parent.get("owner_token")
    parent_level = parent.get("level")
    live_children = _live_child_count(node_address)

    outcomes: list[ServiceOutcome] = []
    for req_path in _pending_requests(outbox_dir):
        outcome = _service_one(node_address, parent_token, parent_level, live_children, req_path)
        # Only a genuinely-NEW child admits against the running in-sweep count — an already-live
        # idempotent .done is in the _live_child_count base already (counting it twice would
        # prematurely trip the cap for fresh requests this sweep).
        if outcome.status in {"spawned", "queued"} and not outcome.already_live:
            live_children += 1
        outcomes.append(outcome)
    return outcomes


def _service_one(node_address: str, parent_token: Optional[str], parent_level: Optional[str],
                 live_children: int, req_path: Path) -> ServiceOutcome:
    """Adjudicate a single request file: parse -> validate -> descent (fail-loud if the parent's level
    is unknown) -> ALREADY-LIVE idempotent resolution -> fan-out cap -> the REAL spawn.

    EVERY exit consumes the request (renames it .done or .rejected-with-reason) — there is no path
    where a request stays a pending ``.json`` and silently does nothing. An UNEXPECTED spawn error is
    caught and surfaced as a visible rejection (the load-bearing "never silent-drop" guarantee — a bare
    raise here would otherwise be swallowed by the daemon's best-effort sweep and strand the request).
    """
    try:
        obj = json.loads(req_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        _consume(req_path, "rejected", f"malformed request: {exc}")
        return ServiceOutcome(str(req_path), "rejected", reason="malformed request")

    ok, reason = validate_request(obj)
    if not ok:
        _consume(req_path, "rejected", reason)
        return ServiceOutcome(str(req_path), "rejected", reason=reason)

    # DESCENT — a child must be STRICTLY deeper than its parent (block same-level + up-level/escalation).
    # An unknown/missing parent level is a FAIL-LOUD refusal: mapping it to a default rank would wave
    # ANY child level through the system's ONLY descent gate (the chokepoint has no gate of its own) —
    # fail-open on corrupt ledger data. An unverifiable descent must refuse visibly, never admit.
    child_level = obj["child_level"]
    parent_rank = _LEVEL_ORDER.get(parent_level)
    if parent_rank is None:
        msg = (f"parent level {parent_level!r} is not a known level {list(_LEVEL_ORDER)} — "
               "descent cannot be verified (refusing: fail-loud, not fail-open)")
        _consume(req_path, "rejected", msg)
        return ServiceOutcome(str(req_path), "rejected", reason=msg)
    if _LEVEL_ORDER[child_level] <= parent_rank:  # direct index is safe: validate_request pinned
        msg = f"child_level {child_level!r} is not deeper than parent level {parent_level!r}"
        _consume(req_path, "rejected", msg)
        return ServiceOutcome(str(req_path), "rejected", reason=msg)

    child_address = compose_child_address(node_address, obj["child_name"])
    child_metadata = _request_spawn_metadata(obj)
    if child_metadata.get("child_purpose") == PURPOSE_TEST_AUTHOR and (
        parent_level != "L4" or child_level != "L5"
    ):
        msg = "purpose='test_author' is only valid for an L4 spawning an L5 test-author child"
        _consume(req_path, "rejected", msg)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=msg)
    if child_metadata.get("child_purpose") == PURPOSE_PLANNING and (
        parent_level != "L2" or child_level != "L3"
    ):
        msg = "purpose='planning' is only valid for an L2 spawning an L3 planning child"
        _consume(req_path, "rejected", msg)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=msg)
    refresh_block = _reject_l4_l5_spawn_for_pending_test_refresh(
        node_address=node_address,
        parent_level=parent_level,
        child_level=child_level,
        child_metadata=child_metadata,
    )
    if refresh_block:
        _consume(req_path, "rejected", refresh_block)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=refresh_block)
    # ALREADY-LIVE idempotent resolution — BEFORE the fan-out cap. The prior spawn succeeded and only
    # the .done rename was lost (the crash window the module docstring promises to replay safely).
    # Resolving it here means (a) no misleading .rejected-at-cap for a child that actually exists and
    # (b) no double-count — this child is already in the _live_child_count base.
    existing_child = ledger.read_binding(child_address)
    unresolved_gate = _unresolved_same_address_gate(existing_child)
    if unresolved_gate is not None:
        _consume(req_path, "rejected", unresolved_gate)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=unresolved_gate)
    stale_phase = _stale_planning_phase_reuse(existing_child, child_address, obj)
    if stale_phase is not None:
        _consume(req_path, "rejected", stale_phase)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=stale_phase)
    if _child_already_live(child_address):
        _consume(req_path, "spawned")
        return ServiceOutcome(str(req_path), "spawned", child_address=child_address, already_live=True)

    client_brief_receipts, intent_block = chokepoint.prepare_client_brief_for_spawn(
        node_address,
        child_address,
        child_level=child_level,
    )
    if intent_block is not None:
        _failure_class, reason = intent_block
        _consume(req_path, "rejected", reason)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=reason)
    if client_brief_receipts:
        child_metadata.update(client_brief_receipts)
        child_metadata["contract_receipts"] = contracts.merge_receipts(
            child_metadata.get("contract_receipts"),
            *client_brief_receipts.values(),
        )

    bind_block = chokepoint.bind_accepted_test_package_for_spawn(
        node_address,
        child_address,
        child_level=child_level,
        child_metadata=child_metadata,
    )
    if bind_block is not None:
        _failure_class, reason = bind_block
        _consume(req_path, "rejected", reason)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=reason)

    refresh_content_block = chokepoint.approved_test_refresh_spawn_block(
        node_address,
        child_address,
        child_level=child_level,
        child_metadata=child_metadata,
    )
    if refresh_content_block is not None:
        _failure_class, reason = refresh_content_block
        _consume(req_path, "rejected", reason)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=reason)

    package_block = chokepoint.accepted_test_package_spawn_block(
        node_address,
        child_address,
        child_level=child_level,
        child_metadata=child_metadata,
    )
    if package_block is not None:
        _failure_class, reason = package_block
        _consume(req_path, "rejected", reason)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=reason)

    # FAN-OUT BACKSTOP — refuse once the parent already holds the cap of live children (visible reject).
    # Reached only by genuinely-NEW children (the already-live pre-check resolved replays above).
    if live_children >= MAX_CHILDREN_PER_PARENT:
        msg = f"parent holds {live_children} live children (cap {MAX_CHILDREN_PER_PARENT})"
        _consume(req_path, "rejected", msg)
        return ServiceOutcome(str(req_path), "rejected", reason=msg)

    try:
        level_config = config.get_level_config(child_level)
        res = chokepoint.register_and_spawn_child(
            node_address, child_address,
            child_level_config=level_config,
            brief_content=obj.get("brief"),  # None -> derive from the pre-authored node (the default)
            expected_parent_owner_token=parent_token,
            child_metadata=child_metadata,
        )
    except Exception as exc:  # noqa: BLE001 — an unexpected spawn error MUST surface, never silently stall
        _consume(req_path, "rejected", f"spawn error: {exc}")
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address,
                              reason=f"spawn error: {exc}")

    if getattr(res, "ok", False):
        _consume(req_path, "spawned")
        return ServiceOutcome(str(req_path), "spawned", child_address=child_address)

    failure = getattr(res, "failure_class", None) or "spawn_failed"
    if failure == "admission_queued":
        _consume(req_path, "spawned")
        return ServiceOutcome(str(req_path), "queued", child_address=child_address, reason=failure)

    # CONCURRENT-WINDOW BACKSTOP — the pre-check above covers the crash-replay case, but a child
    # registered BETWEEN our pre-check and register (an interleaved IPC service-outbox or sweep) makes
    # register lose its single-owner claim BECAUSE the child already exists. Still a SUCCESS, not a
    # rejection — mark it .done, flag it already_live so it is not counted against the in-sweep cap.
    existing_child = ledger.read_binding(child_address)
    unresolved_gate = _unresolved_same_address_gate(existing_child)
    if unresolved_gate is not None:
        _consume(req_path, "rejected", unresolved_gate)
        return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=unresolved_gate)
    if _child_already_live(child_address):
        _consume(req_path, "spawned")
        return ServiceOutcome(str(req_path), "spawned", child_address=child_address, already_live=True)

    _consume(req_path, "rejected", f"spawn refused: {failure}")
    return ServiceOutcome(str(req_path), "rejected", child_address=child_address, reason=failure)


def service_all_outboxes() -> list[ServiceOutcome]:
    """Service every live NON-LEAF node's outbox (the daemon-loop entry; called best-effort per tick).

    Iterates the live binding map; skips terminal nodes and L5 executor leaves (they spawn no children).
    Each node is serviced under its OWN identity, so provenance holds across the whole sweep.
    """
    outcomes: list[ServiceOutcome] = []
    for address, binding in ledger.all_nodes().items():
        if states.is_terminal(binding.get("state")):
            continue
        if binding.get("level") == LEAF_LEVEL:
            continue
        try:
            outcomes.extend(service_outbox(address))
        except Exception:  # noqa: BLE001 — one node's bad outbox must not abort the sweep or starve later nodes
            continue
    return outcomes
