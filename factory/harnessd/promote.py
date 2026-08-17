"""Control-plane promotion / delivery — the ONE sanctioned cross-write-jail action (Increment 17).

Authoritative sources:
  - design/INTAKE-TO-DELIVERY.md §3 (promotion is a control-plane cross-jail write) + Stage 6.
  - design/DAEMON.md §3.2 — the deliverable binding block: ``deliverable_state``
    (planned|active|waiting|completed|blocked|cancelled|delivered|delivery-failed),
    ``write_targets`` (the IN-JAIL source surface), ``delivery_destination`` (the OUT-OF-JAIL target),
    ``delivery_kind`` (filesystem-path | git-remote). ``delivery_destination`` is DISTINCT from
    ``write_targets`` — the jail boundary stays legible; neither is overloaded onto the other.
  - harnessd/executor.py — the SINGLE writer. The deliverable-state write goes through
    ``executor.deliver`` (an own-slice write routed through the one mutation path); NO raw
    ``ledger.write_binding`` second mutation path.
  - harnessd/spawn/chokepoint.py — the §6.3 escalation precedent: a failure emits an L1-readable
    escalation WAL row via the run-ledger (``_emit_spawn_failure_escalation``). The delivery-failed
    path mirrors that seam here (``_emit_delivery_failure_escalation``).

THE INCREMENT — ``promote(node_address, *, accept_signal)``:

  Promotion is performed by ``harnessd`` (the control plane), NOT by any agent, and is GATED on
  L1's explicit delivery trigger plus the current owner-confirmed fidelity playback. Every agent —
  L1 included — is write-jailed to its own
  ``/runtime/`` node subtree (SECURITY §1.3); the delivery destination is OUTSIDE every jail (a
  user filesystem path or a git remote). Crossing that boundary is structurally impossible for a
  jailed agent — it is a control-plane operation. This is the one sanctioned cross-jail write.

  GATE       — proceeds ONLY on an explicit ``accept_signal`` trigger, a complete L1 preliminary
               judgment, and the current immutable owner CONFIRM (or distinctly labelled,
               launch-preauthorized commissioning-delegate CONFIRM). With no trigger, a REJECT
               decision, or missing/stale/rejecting fidelity playback -> NO-OP:
               the destination is untouched and ``/runtime/`` is left intact (gated, never
               speculative).
  ON ACCEPT  — copy the finished deliverable OUT of the node's ``nodes/<path>/`` workspace
               (``addressing.node_dir``) to
               ``delivery_destination``: a filesystem copy-out (``delivery_kind='filesystem-path'``)
               or a real ``git push`` (``delivery_kind='git-remote'``). Then record
               ``deliverable_state=delivered`` + ``delivery_destination`` on the binding via the
               SINGLE writer (``executor.deliver``). ``write_targets`` stays the in-jail source surface.
  ON FAILURE — record ``deliverable_state=delivery-failed`` via the single writer AND ESCALATE (the
               §6.3 run-ledger escalation seam, an L1-readable WAL row).

RESOLVED DETAILS (unspecified by the frozen tests; decided spec-faithfully, surfaced to the
orchestrator — see the module-return + this docstring):

  * SIGNATURE — ``promote(node_address, *, accept_signal) -> PromoteResult`` (a ``NamedTuple`` with
    ``ok`` + structured fields), matching the test's ``_promote_callable`` contract and its
    ``getattr(result, "ok", ...)`` probe.

  * THE DELIVERABLE SUBTREE — the source IS ``addressing.node_dir(node_address)``: the node's
    canonical ``<RUNTIME_ROOT>/nodes/<path>/`` workspace — the SAME dir the jail WORKROOT, the
    chokepoint brief-landing, the detector, and the watchdog all derive (addressing.py's charter:
    ONE nested derivation shared by every consumer so they cannot drift; F8/JSF-03 reconciled
    promote onto it — the old ``proj/{project}`` re-derivation read a path no agent ever writes).
    ``write_targets`` is no longer used for path resolution: it remains the §3.2 in-jail
    jail-surface field, never written (and now never read) by promote. The control-plane dotfiles
    the daemon/agent exchange inside that workspace (``.sign-off.*`` / ``.signal.*`` / ``.inbox.*``,
    F19) are harness machinery, not product — the filesystem copy-out EXCLUDES them (the git-remote
    variant ships the committed tree, which never contains them: the harness seeds them unstaged).

  * GATE SEMANTICS — ``accept_signal`` is the explicit deliberate delivery trigger, never the
    authority. It must be node-bound and carry ``decision == 'accept'``. Authority comes only from
    the current content-addressed fidelity question and immutable answer verified by
    ``fidelity_playback``.

  * FILESYSTEM COPY-OUT — a real recursive ``shutil.copytree`` of the source tree to the normalized
    ``delivery_destination`` (parents created). When the frozen intent-spec carries §8, that row is
    authoritative at promote time and can supersede a stale cached binding destination. A genuine OS
    failure (e.g. the destination parent is a regular file) raises and routes to the delivery-failed
    path; no failure is mocked.

  * REPAIR RETRY — a caller may supply ``delivery_destination_override`` only after the binding is
    already ``deliverable_state='delivery-failed'``. The override is normalized through the same
    destination parser and supersedes the stale failed destination for that retry, while the prior
    failed target remains recorded in ``delivery_failed_targets``. This keeps repair in the
    control-plane path instead of asking an agent to edit frozen intent specs or live bindings.

  * GIT PUSH MECHANICS — the ``/runtime/`` deliverable tree is a real git work tree (committed). The
    promote runs ``git push <delivery_destination> HEAD:refs/heads/main`` from the source tree, in an
    ISOLATED git env (``GIT_CONFIG_GLOBAL=/dev/null`` / ``GIT_CONFIG_SYSTEM=/dev/null`` + a fixed
    harnessd author/committer identity), so the push does not depend on the user's git config and is
    deterministic. A non-zero ``git`` exit routes to the delivery-failed path. FORK: push target ref
    is ``refs/heads/main`` (the deliverable's default branch); ``HEAD:refs/heads/main`` pushes whatever
    the source HEAD is onto the remote's ``main``.

  * THE WRITE PATH — ``executor.deliver`` (own-slice, no lifecycle-state change, no second mutation
    path). The promote does NOT advance the lifecycle ``state`` (the project stays ``done`` — accepted
    and finished); it stamps only the deliverable block. ``executor.deliver`` journals a WAL row
    (``actor='harnessd'``) and advances ``last_applied_seq``, so the delivery is audited and
    attributable to the control plane, never an agent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

from . import (
    addressing,
    clock,
    executor,
    fidelity_playback,
    ledger,
    return_contract,
)


# ---------------------------------------------------------------------------
# E3 — the intake gate at the delivery edge (enforcement spine, 2026-06-11).
# Deterministic reads of the node's frozen intent-spec (client-brief/intent-spec.md,
# fallback intent-spec.md): the §8 delivery destination (AUTHORITATIVE when present;
# explicit `in-place` = the sanctioned no-external-delivery) and the
# FREEZE-ON-PENDING block (a load-bearing requirement row still `pending`
# reflect-back REFUSES accept — PLAN-ALIGNMENT-GATE: nothing is frozen against an
# unconfirmed foundation; intent-spec-contract §2 Reflect-back status).
# ---------------------------------------------------------------------------

IN_PLACE = "in-place"
# A requirements-table row carrying BOTH a minted R-id and a `pending` cell.
_PENDING_ROW = re.compile(r"^.*\bR-\d+(?:\.\d+)*\b.*\bpending\b.*$", re.IGNORECASE | re.MULTILINE)
_DEST_ROW = re.compile(r"^\|?\s*`?Destination`?\s*\|\s*(.+?)\s*\|?\s*$", re.IGNORECASE | re.MULTILINE)
_DEST_CODE_SPAN = re.compile(r"`([^`]+)`")
_DEST_PAREN_ABS_PATH = re.compile(r"\((/[^)]+)\)")
_KIND_TOKEN = re.compile(r"\b(filesystem-path|git-remote|in-place)\b", re.IGNORECASE)
def _destination_token(raw: str) -> str:
    """Extract the machine-usable destination from a human-facing markdown table cell."""
    value = str(raw or "").strip()
    if value.endswith("|"):
        value = value[:-1].rstrip()
    spans = [span.strip() for span in _DEST_CODE_SPAN.findall(value) if span.strip()]
    if spans:
        for span in spans:
            if span.startswith("/"):
                return span
        for span in spans:
            if span.startswith("~"):
                return span
        return spans[0]
    paren_abs = [span.strip() for span in _DEST_PAREN_ABS_PATH.findall(value) if span.strip()]
    if paren_abs:
        return paren_abs[-1]
    value = value.strip("`").strip()
    value = re.sub(r"\s+\([^)]*\)\s*$", "", value).strip()
    return value


def _normalize_delivery_destination(raw: Optional[str], delivery_kind: Optional[str]) -> Optional[str]:
    """Normalize a binding/spec delivery destination before promotion uses or records it."""
    if raw is None:
        return None
    value = _destination_token(str(raw))
    if not value:
        return value
    if IN_PLACE in value.lower():
        return IN_PLACE
    if delivery_kind != "git-remote":
        return str(Path(value).expanduser())
    return value


def _append_delivery_failed_target(
    binding: dict,
    *,
    destination: Optional[str],
    delivery_source: Optional[str],
    reason: str,
) -> list[dict]:
    existing = binding.get("delivery_failed_targets") or []
    targets = [dict(item) for item in existing if isinstance(item, dict)]
    targets.append({
        "destination": destination,
        "delivery_source": delivery_source,
        "reason": reason,
        "failed_at": clock.now_utc(),
    })
    return targets


def _find_intent_spec(node_address: str) -> tuple:
    """Compatibility wrapper over Q6's one shared project-artifact locator."""
    return fidelity_playback.find_intent_spec(node_address)


def _intake_gate(node_address: str) -> tuple:
    """Read the frozen intent-spec (when present): (derived_destination, derived_kind,
    refusal_errors). Absent spec -> (None, None, []) — derivation has nothing to read and the
    freeze block nothing to enforce (a binding-stamped destination then carries the promote,
    the pre-E3 behavior). An AMBIGUOUS portfolio (LR-22) is a refusal, never a guess."""
    spec, ambiguity = _find_intent_spec(node_address)
    if ambiguity:
        return None, None, [ambiguity]
    if spec is None:
        return None, None, []
    try:
        text = spec.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None, []

    errors: list = []
    pending = _PENDING_ROW.findall(text)
    if pending:
        errors.append(
            f"FREEZE-ON-PENDING: the frozen intent-spec at {spec} carries {len(pending)} "
            f"load-bearing requirement row(s) still pending reflect-back confirmation — accept "
            f"refuses to deliver on an unconfirmed foundation (first: {pending[0].strip()[:140]!r})"
        )

    derived_dest: Optional[str] = None
    derived_kind: Optional[str] = None
    dest_match = _DEST_ROW.search(text)
    raw_dest: Optional[str] = None
    if dest_match:
        raw_dest = dest_match.group(1).strip()
    if raw_dest and IN_PLACE in raw_dest.lower():
        derived_dest, derived_kind = IN_PLACE, IN_PLACE
    elif raw_dest:
        kind_match = _KIND_TOKEN.search(text)
        derived_kind = kind_match.group(1).lower() if kind_match else None
        derived_dest = _normalize_delivery_destination(raw_dest, derived_kind)
    return derived_dest, derived_kind, errors


def _find_fidelity_judgment(node_address: str) -> tuple:
    """Compatibility wrapper over Q6's one shared project-artifact locator."""
    return fidelity_playback.find_judgment(node_address)


def _extract_fidelity_verdict(text: str) -> Optional[str]:
    """Compatibility reader for pre-Q6 historical fidelity artifacts."""
    return fidelity_playback.extract_legacy_verdict(text)


def _fidelity_gate(node_address: str) -> list[str]:
    """Require L1 preliminary evidence plus the current owner-confirmed playback answer."""
    return fidelity_playback.promotion_blockers(node_address)


# ---------------------------------------------------------------------------
# Result type (resolved signature, surfaced above).
# ---------------------------------------------------------------------------

class PromoteResult(NamedTuple):
    """The outcome of a ``promote`` call.

    ``ok``                  — True iff the deliverable was delivered (copy-out / push landed AND the
                              binding was stamped ``delivered``). False on a gated no-op OR a failure.
    ``delivered``           — True iff bytes landed at the destination (== ok on the happy path).
    ``deliverable_state``   — the deliverable_state written on the binding (or the live value on a
                              no-op): 'delivered' | 'delivery-failed' | <unchanged>.
    ``delivery_destination``— the out-of-jail target the deliverable was promoted to (or None on a no-op).
    ``errors``              — abort/failure reasons; empty on success.
    """

    ok: bool
    delivered: bool
    deliverable_state: Optional[str]
    delivery_destination: Optional[str]
    errors: list
    playback_authorization: Optional[str] = None


# ---------------------------------------------------------------------------
# The deliberate delivery trigger. ACCEPT -> evaluate owner-final gate; None / non-mapping /
# any non-'accept' decision -> NO-OP.
# ---------------------------------------------------------------------------

def _is_accept(accept_signal, node_address) -> bool:
    """True iff ``accept_signal`` deliberately requests delivery FOR THIS node.

    The gate is per-PROJECT (INTAKE-TO-DELIVERY §3 / DAEMON §7 — L1 accepts a specific project's
    deliverable). So the accept must be BOUND to the node being promoted: an accept for project A must
    NOT promote project B. We require ``accept_signal['node_address'] == node_address`` — a missing or
    mismatched node binding HOLDS the gate (no-op), the secure default for a cross-jail-write trigger.
    """
    if not isinstance(accept_signal, dict):
        return False
    if accept_signal.get("decision") != "accept":
        return False
    return accept_signal.get("node_address") == node_address


# ---------------------------------------------------------------------------
# Source-tree resolution — the in-jail deliverable subtree under /runtime/.
# ---------------------------------------------------------------------------

def _source_tree(node_address: str, delivery_source: Optional[str] = None) -> tuple[Path, Optional[str]]:
    """The in-jail deliverable subtree for promotion.

    Default source is ``addressing.node_dir(node_address)`` — the node's canonical
    ``<RUNTIME_ROOT>/nodes/<path>/`` workspace, the ONE dir every agent actually writes (F8/JSF-03).
    When a promote request supplies ``delivery_source``, it selects a product surface under that
    workspace. The override is resolved and fenced inside the node root; it cannot point outside the
    write-jail tree.

    Address-shape-agnostic by construction: ``node_dir`` nests whatever path the address carries
    (``proj/demo-widget#exec`` and ``L1/demo#exec`` both resolve), so promote does not care which
    composition produced the project address. Fail-loud absent-source guard: a workspace no agent
    ever wrote is a precise, journaled fault (the caller routes it to delivery-failed + §6.3) —
    never an opaque copytree FileNotFoundError or a git push from a nonexistent cwd.
    """
    if ledger.RUNTIME_ROOT is None:
        raise RuntimeError(
            "promote source path is not configured: bind ledger.RUNTIME_ROOT (the /runtime/ jail root)"
        )
    source = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
    if not source.is_dir():
        raise ValueError(
            f"deliverable source tree {source} does not exist — no agent ever wrote this node's "
            "workspace (nodes/<path>/, addressing.node_dir)"
        )
    if not delivery_source:
        return source, None
    raw_source = str(delivery_source).strip()
    if not raw_source:
        return source, None
    source_root = source.resolve()
    requested = Path(raw_source).expanduser()
    candidate = requested if requested.is_absolute() else source / requested
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            f"delivery_source {raw_source!r} must resolve inside node workspace {source_root}"
        ) from exc
    if not resolved.is_dir():
        raise ValueError(
            f"delivery_source {raw_source!r} resolved to {resolved}, but it is not a directory"
        )
    relative_text = str(relative) if str(relative) != "." else "."
    return resolved, relative_text


# ---------------------------------------------------------------------------
# Cross-jail copy-out / push — the control-plane boundary crossing.
# ---------------------------------------------------------------------------

def _git_env(source_tree: Path) -> dict:
    """An isolated git env so the push does not depend on the user's git config and is deterministic."""
    return {
        "GIT_AUTHOR_NAME": "harnessd",
        "GIT_AUTHOR_EMAIL": "harnessd@example.invalid",
        "GIT_COMMITTER_NAME": "harnessd",
        "GIT_COMMITTER_EMAIL": "harnessd@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(source_tree),
        "PATH": os.environ.get("PATH", ""),
    }


# The control-plane machinery the daemon/agent exchange INSIDE the node workspace. Harness
# machinery, NOT product: the copy-out excludes it all (shutil.ignore_patterns fnmatches
# BASENAMES at every level — dirs included — so a child node's machinery nested under a
# coordinator's deliverable is excluded too):
#   * the F19 dotfiles — the sign-off handshake, the per-seat terminal signal (+ the RR-2
#     ``.signal.<seat>.json.invalid`` quarantine, covered by the same ``.signal.*`` glob), and
#     the wake inbox;
#   * ``.harness-outbox`` (INT-3) — the FORK-SPAWN-CHANNEL spawn-request dir (outbox.
#     OUTBOX_DIRNAME) lives in the SAME workroot the copy sources, including every nested child
#     node's; its request JSONs + consumed .done/.rejected markers were shipping in the
#     deliverable — the exact class the F8 exclusion exists to keep out;
#   * ``.sandbox-profiles`` (INT-3) — claude_code._write_profile's rendered-.sb fallback dir can
#     land inside the workroot;
#   * ``.*.tmp`` (INT-3) — store.atomic_replace residue (``.<name>.tmp``): only present after a
#     crash mid-replace, but never deliverable bytes.
_CONTROL_PLANE_DOTFILE_PATTERNS = (
    ".sign-off.*", ".signal.*", ".inbox.*",
    ".harness-outbox", ".sandbox-profiles", ".*.tmp",
)


def _copy_out_filesystem(source_tree: Path, destination: str) -> None:
    """Real recursive copy-out of the deliverable tree to a filesystem destination (parents created).

    EXCLUDES the F19 control-plane dotfiles (``.sign-off.*`` / ``.signal.*`` / ``.inbox.*``) — they
    live in the same node dir the copy sources but are harness machinery, never deliverable bytes.
    Raises on a genuine OS failure (e.g. the destination's parent is a regular file) — the caller
    routes that to the delivery-failed path. No failure is mocked.
    """
    dest = Path(destination).expanduser()
    # copytree (Python 3.8+: dirs_exist_ok) writes the WHOLE subtree. A failure to create the
    # destination (parent is a file) raises NotADirectoryError/OSError — the genuine failure the
    # delivery-failed path is for.
    shutil.copytree(
        source_tree,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*_CONTROL_PLANE_DOTFILE_PATTERNS),
    )


def _push_git_remote(source_tree: Path, destination: str) -> None:
    """Real ``git push`` of the deliverable work tree to the captured remote. Raises on a non-zero exit."""
    result = subprocess.run(
        [os.environ.get("HARNESSD_GIT", "git"), "push", destination, "HEAD:refs/heads/main"],
        cwd=str(source_tree),
        env=_git_env(source_tree),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git push to {destination!r} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def _promote_out(source_tree: Path, destination: str, delivery_kind: Optional[str]) -> None:
    """Dispatch the boundary crossing by ``delivery_kind``. Raises on any failure."""
    if delivery_kind == "git-remote":
        _push_git_remote(source_tree, destination)
    else:
        # Default / 'filesystem-path': a real filesystem copy-out.
        _copy_out_filesystem(source_tree, destination)


# ---------------------------------------------------------------------------
# The §6.3 escalation seam — an L1-readable delivery-failure WAL row (chokepoint precedent).
# ---------------------------------------------------------------------------

def _emit_delivery_failure_escalation(node_address: str, destination: Optional[str], reason: str) -> None:
    """Append an L1-readable delivery-failure escalation row to the run-ledger (the §6.3 seam).

    Mirrors ``chokepoint._emit_spawn_failure_escalation``: a DIRECT WAL append (actor='harnessd',
    hard-coded by the ledger) naming the node + the failure, so an L1 reconcile reader sees the
    ``delivery_failed`` event. Best-effort: a journaling hiccup must not mask the underlying failure
    (the result already carries it, and the binding is stamped delivery-failed by the single writer).
    Routed through ``executor.journal`` (SWL-01): seq allocation + append under the EX lock.
    """
    try:
        executor.journal(
            node_address,
            # DISTINCT event from the deliverable_state stamp (executor.deliver event='delivery_failed'),
            # so the §6.3 escalation row is INDEPENDENTLY assertable from the state-record row (they are
            # two separate concerns: the binding stamp vs the L1-readable escalation).
            event="delivery_failed_escalation",
            from_state="done",
            to_state="done",  # lifecycle unchanged — a delivery is orthogonal to the lifecycle axis
            binding_delta={"deliverable_state": "delivery-failed", "delivery_destination": destination,
                           "escalation": "delivery_failed"},
            summary=(
                f"delivery-failure escalation -> L1: node {node_address} failed to promote to "
                f"{destination!r} ({reason}); deliverable_state=delivery-failed (§6.3)"
            ),
        )
    except Exception:
        # The result + the delivery-failed binding stamp already carry the failure; a WAL hiccup
        # must not swallow it.
        return None


# ---------------------------------------------------------------------------
# LR-23 (remedy a) — the gate-evidence drift check at the accept path. Detection
# only, NEVER blocking: the operator decides what a drifted report means.
# ---------------------------------------------------------------------------

def _journal_report_drift_if_any(node_address: str, binding: dict) -> None:
    """Compare the CURRENT report.md hash against the collapse-time GATE EVIDENCE-STAMP and
    journal ONE ``report_drift`` row on mismatch (LR-23 remedy a, user-ruled 2026-06-12).

    The stamp (``report_sha256``/``report_bytes``, frozen onto the binding by
    ``chokepoint.collapse`` at the accepted-DONE collapse) is the FACT of what the gate approved;
    the collapsed agent's pane stays alive (LT-4) and can keep writing (Run-2 ws-3: report.md
    edited 11s AFTER its accepted DONE), so the report being delivered may not be the report the
    gate saw. On a hash mismatch — including a report that has VANISHED since the gate (current
    hash None) — ONE run-ledger row lands naming BOTH hashes, and the promote PROCEEDS:
    detection, never refusal (no seal, no reap — options b/c explicitly not ruled; this row is
    also the post-collapse-mutation DATA the stronger remedies would be judged on).

    Best-effort end to end: an unstamped binding (a pre-stamp node, or one that never passed a
    DONE gate) has nothing to compare — silent skip; a journaling hiccup never blocks the
    delivery (mirrors the §6.3 escalation seam's posture). The hash is read through
    ``return_contract.report_stamp`` — the SAME report.md derivation the E2 walker and the
    collapse stamp use, so the three readers cannot drift apart.
    """
    try:
        stamped_sha = binding.get("report_sha256")
        if not stamped_sha:
            return
        current = return_contract.report_stamp(node_address)
        current_sha = current.get("report_sha256") if current else None
        if current_sha == stamped_sha:
            return
        executor.journal(
            node_address,
            event="report_drift",
            from_state=binding.get("state"),
            to_state=binding.get("state"),  # lifecycle unchanged — drift is an audit fact
            binding_delta={
                "report_sha256_at_gate": stamped_sha,
                "report_bytes_at_gate": binding.get("report_bytes"),
                "report_sha256_at_promote": current_sha,
                "report_bytes_at_promote": current.get("report_bytes") if current else None,
            },
            summary=(
                f"report drift at promote: {node_address} report.md no longer matches the "
                f"accepted-DONE gate stamp (gate sha256 {stamped_sha}, current "
                f"{current_sha or 'ABSENT'}) — the report being delivered is not the report the "
                f"gate approved (LR-23); promote PROCEEDS, the operator decides"
            ),
        )
    except Exception:  # noqa: BLE001 — detection must never block or crash the gated delivery
        return None


# ---------------------------------------------------------------------------
# promote() — the gated control-plane op.
# ---------------------------------------------------------------------------

def promote(
    node_address: str,
    *,
    accept_signal,
    delivery_source: Optional[str] = None,
    delivery_destination_override: Optional[str] = None,
    delivery_kind_override: Optional[str] = None,
) -> PromoteResult:
    """Gated control-plane promote-out-of-/runtime/ (Increment 17). See module docstring for the contract.

    GATE (Stage 5): proceeds ONLY on an explicit node-bound delivery trigger plus a complete
    preliminary fidelity artifact and the current immutable owner/delegate CONFIRM. Missing or
    stale authority is a NO-OP (destination untouched, ``/runtime/`` intact). ON AUTHORIZED ACCEPT:
    copy-out / push the deliverable to the captured ``delivery_destination``
    or to a repair-only override after a prior delivery failure, then stamp
    ``deliverable_state=delivered`` + ``delivery_destination`` via the SINGLE writer
    (``executor.deliver``). ON FAILURE: stamp ``deliverable_state=delivery-failed`` + record the
    failed destination + escalate.
    """
    binding = ledger.read_binding(node_address)
    if binding is None:
        # No such node — nothing to promote. A gated no-op (no destination, no source touched).
        return PromoteResult(
            ok=False,
            delivered=False,
            deliverable_state=None,
            delivery_destination=None,
            errors=[f"no binding for node {node_address!r}: cannot promote an absent node"],
        )

    destination = binding.get("delivery_destination")
    delivery_kind = binding.get("delivery_kind")

    # The explicit delivery trigger remains node-bound. Owner-final authorization is
    # checked independently below by the fidelity-playback gate.
    if not _is_accept(accept_signal, node_address):
        return PromoteResult(
            ok=False,
            delivered=False,
            deliverable_state=binding.get("deliverable_state"),
            delivery_destination=destination,
            errors=["gate held: no deliberate delivery trigger FOR THIS node "
                    "(decision != 'accept' or the trigger is not bound to this node_address) "
                    "— promote is a no-op"],
        )

    fidelity_errors = _fidelity_gate(node_address)
    if fidelity_errors:
        return PromoteResult(
            ok=False,
            delivered=False,
            deliverable_state=binding.get("deliverable_state"),
            delivery_destination=destination,
            errors=fidelity_errors,
        )
    playback_authorization, _authorization_errors = (
        fidelity_playback.promotion_authorization(node_address)
    )

    has_retry_destination = delivery_destination_override is not None
    if has_retry_destination and binding.get("deliverable_state") != "delivery-failed":
        return PromoteResult(
            ok=False,
            delivered=False,
            deliverable_state=binding.get("deliverable_state"),
            delivery_destination=destination,
            errors=[
                "DELIVERY-RETRY-REQUIRES-FAILED: an explicit delivery_destination override is a "
                "repair path and is valid only after deliverable_state='delivery-failed'"
            ],
        )

    # --- E3: THE INTAKE GATE at the delivery edge. The freeze block REFUSES (a refusal, not a
    # delivery failure: the artifact is unconfirmed, not the delivery broken — deliverable_state
    # stays untouched); when the frozen intent-spec carries §8, that artifact is authoritative for
    # the destination at promote time, so a stale cached binding destination cannot trap repair.
    # A harness-owned retry destination may supersede the stale destination only after a recorded
    # delivery failure; the same intake gate still runs to enforce freeze/ambiguity blockers. ---
    derived_dest, derived_kind, gate_errors = _intake_gate(node_address)
    if gate_errors:
        return PromoteResult(
            ok=False,
            delivered=False,
            deliverable_state=binding.get("deliverable_state"),
            delivery_destination=destination,
            errors=gate_errors,
        )
    if has_retry_destination:
        delivery_kind = delivery_kind_override or delivery_kind
        destination = _normalize_delivery_destination(delivery_destination_override, delivery_kind)
        if destination == IN_PLACE:
            delivery_kind = IN_PLACE
    elif derived_dest:
        destination, delivery_kind = derived_dest, derived_kind
    elif destination:
        destination = _normalize_delivery_destination(destination, delivery_kind)

    # --- LR-23 (remedy a): the accept is committed to delivering — compare the CURRENT report.md
    # hash against the accepted-DONE gate stamp; on mismatch journal ONE report_drift row naming
    # both hashes and PROCEED (detection only, never a refusal — the operator decides). ---
    _journal_report_drift_if_any(node_address, binding)

    # --- ON ACCEPT: cross the jail boundary (copy-out / push), then record via the single writer. ---
    # EVERYTHING that can fail (source resolution / an absent workspace, a missing destination, the
    # copy-out/push) lives
    # INSIDE the try, so a precondition fault routes to the delivery-failed path + escalation — NEVER
    # an uncaught crash out of the gated promote (a delivery crash must be a journaled failure, not a
    # raised exception the daemon has to catch).
    delivery_source_record: Optional[str] = None
    try:
        if not destination:
            # The gate passed but intake never captured a delivery_destination (intent-spec §8) — a
            # real precondition fault: fail loud + escalate, do NOT attempt a None-destination copy.
            raise ValueError(
                "no delivery_destination captured at intake (intent-spec §8) — cannot promote"
            )
        if destination == IN_PLACE:
            # The sanctioned no-external-delivery (intent-spec-contract §8): the deliverable stays
            # in the node; promote stamps delivered WITHOUT a cross-jail copy.
            pass
        else:
            source_tree, delivery_source_record = _source_tree(node_address, delivery_source)
            _promote_out(source_tree, destination, delivery_kind)
    except Exception as exc:  # a GENUINE precondition / copy-out / push failure (no mock)
        # ON FAILURE: record delivery-failed via the single writer + escalate (the §6.3 seam).
        failed_targets = _append_delivery_failed_target(
            binding,
            destination=destination,
            delivery_source=delivery_source_record or delivery_source,
            reason=str(exc),
        )
        executor.deliver(
            node_address,
            deliverable_state="delivery-failed",
            delivery_destination=destination,
            delivery_source=delivery_source,
            extra_delta={"delivery_failed_targets": failed_targets},
            expected_owner_token=None,  # unfenced control-plane write (the daemon is the single writer)
            event="delivery_failed",
            summary=f"promote failed: {exc} (deliverable_state=delivery-failed, §3.2)",
        )
        _emit_delivery_failure_escalation(node_address, destination, str(exc))
        return PromoteResult(
            ok=False,
            delivered=False,
            deliverable_state="delivery-failed",
            delivery_destination=destination,
            errors=[f"promote failed: {exc}"],
        )

    # The bytes landed at the out-of-jail destination. Record delivered via the SINGLE writer:
    # deliverable_state=delivered + delivery_destination; write_targets is NEVER touched here.
    write_result = executor.deliver(
        node_address,
        deliverable_state="delivered",
        delivery_destination=destination,
        delivery_source=delivery_source_record,
        expected_owner_token=None,  # unfenced control-plane write (the daemon is the single writer)
        event="delivered",
        summary=f"promote: deliverable delivered to {destination!r} (deliverable_state=delivered, §3.2)",
    )
    if not write_result.ok:
        # The boundary crossing landed but the journaled stamp aborted — surface it, do NOT claim ok.
        return PromoteResult(
            ok=False,
            delivered=True,
            deliverable_state=binding.get("deliverable_state"),
            delivery_destination=destination,
            errors=["delivery landed but the single-writer state record aborted: "
                    + "; ".join(write_result.errors)],
        )

    return PromoteResult(
        ok=True,
        delivered=True,
        deliverable_state="delivered",
        delivery_destination=destination,
        errors=[],
        playback_authorization=(
            playback_authorization.get("label")
            if playback_authorization
            else None
        ),
    )
