"""FROZEN acceptance — Increment 17: control-plane promotion / delivery (promote-out-of-/runtime/).

Authoritative sources (transcribed, not recalled):
  - harnessd/IMPLEMENTATION-PLAN.md — the Increment-17 Done-test (lines 833-857).
  - design/INTAKE-TO-DELIVERY.md §3 (promotion is a control-plane cross-jail write) + Stage 6.
  - design/DAEMON.md §3.2 — the deliverable binding block: deliverable_state
    (planned|active|waiting|completed|blocked|cancelled|delivered|delivery-failed),
    write_targets (the IN-JAIL source surface), delivery_destination (the OUT-OF-JAIL target),
    delivery_kind (filesystem-path | git-remote). delivery_destination is DISTINCT from
    write_targets — the jail boundary stays legible; do NOT overload one onto the other.
  - harnessd/executor.py — the SINGLE writer (executor.transition; commit is private). No second
    mutation path. Every committed change journals a WAL row with actor='harnessd' and advances
    last_applied_seq.
  - harnessd/spawn/chokepoint.py — the collapse / escalation precedent (a failure emits an L1
    escalation WAL row via the run-ledger; _emit_spawn_failure_escalation is the §6.3 seam).

THE INCREMENT (the one sanctioned cross-write-jail action):
  promote() is a harnessd op, GATED on owner-final fidelity playback for a project, that copies the finished
  deliverable OUT of the node's gitignored nodes/<path>/ workspace (addressing.node_dir) TO the delivery destination
  captured at intake in the frozen intent-spec (a filesystem user-path or a git remote). Agents
  CANNOT do this — every agent is write-jailed to its /runtime/ node subtree and the destination
  is OUTSIDE every jail; only the control plane (harnessd) may cross it, and only on accept.

  GATE        — proceeds ONLY on confirmed playback plus a deliberate trigger. Without either -> NO-OP: the
                destination is untouched and /runtime/ is left intact (gated, never speculative).
  ON ACCEPT   — copy-out (delivery_kind=filesystem-path) or push (git-remote) the finished
                deliverable to delivery_destination; write deliverable_state=delivered on the
                binding via the SINGLE writer (executor.transition — no second mutation path);
                delivery_destination records the target; write_targets stays the in-jail source.
  ON FAILURE  — deliverable_state=delivery-failed + an escalation (the §6.3 run-ledger seam).

  Project teardown/reclaim of /runtime/ AFTER delivery is DEFERRED (register D7) — NOT tested here.

BIAS TO REAL (Lesson 7): a real on-disk nodes/<path>/ deliverable tree; a real temp-dir filesystem
destination (assert the deliverable BYTES land there); the REAL executor records deliverable_state;
the git-remote variant uses a REAL local bare git repo (real `git init --bare` + real `git push`)
so the push is genuinely exercised. No mock of the file/git boundary. No model usage.
"""

from __future__ import annotations

import copy
import importlib
import subprocess

import pytest

from harnessd import addressing, executor, fencing, fidelity_playback, ledger


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so the REAL executor's
# pathless ledger calls (read_binding / append_wal / write_binding) AND the EX
# lock all land under the test tree. Restores the prior value (no cross-test
# leak). This is the same fixture precedent as test_chokepoint.py / test_executor.py.
# ===========================================================================

@pytest.fixture
def runtime(tmp_path):
    # The /runtime/ jail root is a DISTINCT subdir of tmp_path, NOT tmp_path itself: the delivery
    # destination (a sibling under tmp_path, e.g. tmp_path/"delivery-out") must be genuinely OUTSIDE
    # the /runtime/ tree for the cross-jail-boundary assertions to mean anything. Binding RUNTIME_ROOT
    # to tmp_path directly would put every destination INSIDE the jail and make those assertions vacuous.
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = runtime_root
    try:
        yield runtime_root
    finally:
        ledger.RUNTIME_ROOT = previous


# ===========================================================================
# Module-resolution seam. promote lives in harnessd/promote.py (a NEW module —
# the ONE new build artifact of Increment 17). We import it lazily so the RED
# run fails with a clear "module not built yet" rather than a collection error.
# ===========================================================================

def _promote_module():
    """Import harnessd.promote, FAILING LOUDLY with the contract if it is not built.

    The Increment-17 deliverable is a harnessd promotion op. The plan (§3 module table style)
    pins it as harnessd/promote.py exposing a ``promote`` callable. If the module/callable is
    absent the test tells the implementer the exact contract rather than dying on an opaque
    ImportError.
    """
    try:
        return importlib.import_module("harnessd.promote")
    except ImportError as exc:  # pragma: no cover - RED guidance path
        raise AssertionError(
            "Increment 17 not built: expected module ``harnessd/promote.py`` exposing a "
            "control-plane ``promote(node_address, *, accept_signal)`` op (the one sanctioned "
            "cross-write-jail action, gated on owner-final playback; INTAKE-TO-DELIVERY §3 / "
            "IMPLEMENTATION-PLAN Increment-17 Done-test). Underlying import error: "
            f"{exc!r}"
        ) from exc


def _promote_callable():
    module = _promote_module()
    if not hasattr(module, "promote"):
        raise AssertionError(
            "harnessd.promote is missing the ``promote`` op: expected "
            "``promote(node_address, *, accept_signal) -> result`` (gated control-plane "
            "promote-out-of-/runtime/, INTAKE-TO-DELIVERY §3)."
        )
    return module.promote


# ===========================================================================
# Delivery-trigger builders. The owner-final gate is exercised through the
# accept and reject signals as small dicts the promote op consumes; the impl is
# free to read whatever shape it pins, but the gate semantics are fixed:
#   accept  -> proceed;  reject/none -> NO-OP.
# We pass an EXPLICIT, self-describing accept object so a wrong impl that ignores
# the gate (speculative promote) is caught by the reject-path no-op test.
# ===========================================================================

def _accept_signal(node_address):
    """An L1 deliberate delivery trigger for the project after owner confirmation."""
    return {
        "decision": "accept",
        "level": "L1",
        "node_address": node_address,
        "acceptance_ref": "client-brief/intent-spec.md",
        "note": "intent-fidelity accept (Stage 5)",
    }


def _reject_signal(node_address):
    """An L1 delivery trigger carrying REJECT (must NOT promote)."""
    return {
        "decision": "reject",
        "level": "L1",
        "node_address": node_address,
        "acceptance_ref": "client-brief/intent-spec.md",
        "note": "intent-fidelity reject (Stage 5) — bounded re-do",
    }


# ===========================================================================
# Binding seeding — write DIRECTLY through the REAL ledger (the seeding path the
# whole suite uses: ledger.write_binding(map, _lock_held=True)). The node is an
# ACCEPTED project node: lifecycle state=done (the deliverable is finished and
# accepted), deliverable_state=completed (awaiting delivery), with the deliverable
# binding block carrying the in-jail write_targets + the out-of-jail
# delivery_destination/delivery_kind captured at intake.
# ===========================================================================

PROJECT = "demo-widget"
NODE = "proj/demo-widget#exec"
PARENT = "root#exec"
SUBAGENT = "subagent-promote01"
SESSION = "sess-uuid-promote-0001"


def _binding(
    *,
    node_address=NODE,
    state="done",
    deliverable_state="completed",
    generation=5,
    lease_epoch=2,
    delivery_destination=None,
    delivery_kind=None,
    write_targets=None,
    extra=None,
):
    token = fencing.mint_owner_token(node_address, SUBAGENT, SESSION, lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": PARENT,
        "level": "L1",
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "terminal",
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": None,
        # --- the §3.2 deliverable binding block ---
        "deliverable_state": deliverable_state,
        "stop_condition": "demo-widget passes acceptance.md",
        "write_targets": (
            write_targets if write_targets is not None else ["proj/demo-widget/"]
        ),  # the IN-JAIL source surface (must stay this, never overloaded with the destination)
        "evidence_refs": ["report.md"],
        "acceptance_ref": "client-brief/intent-spec.md",
        "delivery_destination": delivery_destination,  # captured at intake (intent-spec §8)
        "delivery_kind": delivery_kind,
    }
    if extra:
        rec.update(extra)
    return rec, token


def _confirm_current_playback(node_address=NODE):
    posted = fidelity_playback.create_question(node_address)
    if posted.get("ok"):
        fidelity_playback.answer_question(
            node_address,
            question_id=posted["question_id"],
            decision="confirm",
            note="Owner confirms this exact test playback.",
        )
    return posted


def _seed(binding, *, owner_confirmed=True):
    ledger.write_binding({binding["node_address"]: copy.deepcopy(binding)}, _lock_held=True)
    if owner_confirmed:
        _confirm_current_playback(binding["node_address"])


def _read(node=NODE):
    return ledger.read_binding(node)


# ===========================================================================
# Real on-disk /runtime/ tree builder. The finished deliverable lives INSIDE the
# node's gitignored nodes/<path>/ workspace (addressing.node_dir — the canonical
# in-jail source surface every agent actually writes; F8 de-mask of the stale
# pre-FORK-NODE-NESTING proj/{project} layout). We synthesize a real multi-file
# tree with distinctive bytes so the copy-out / push is asserted on the ACTUAL
# bytes, not a placeholder.
# ===========================================================================

DELIVERABLE_FILES = {
    "README.md": "# Demo Widget\n\nThe finished, accepted deliverable.\n",
    "src/widget.py": "def widget():\n    return 'PROMOTED-DELIVERABLE-MARKER-7f3a'\n",
    "src/util/helpers.py": "HELPER = 'nested-helper-payload'\n",
    "acceptance.md": "All requirements satisfied.\n",
}


DEFAULT_INTENT_SPEC = """# Intent Spec

## Outcomes
| Outcome ID | Outcome |
|---|---|
| O-001 | The recipient can run the demo widget. |

## Requirements
| ID | Requirement | Tag | MNF | Reflect-back status |
|---|---|---|---|---|
| R-001 | Render the widget | decided | YES | confirmed |
"""


FIDELITY_JUDGMENT = """# Fidelity Judgment

- Asked: Build the demo widget described by the frozen intent-spec.
- Delivered: The demo widget deliverable is present for the client journey.
- Deviations: None material.

Preliminary Verdict: accept

## Outcome Playback
| Outcome ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| O-001 | Opened the demo widget | Recipient-visible widget rendered | acceptance.md | accept |

## MNF Playback
| MNF ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| R-001 | Exercised invalid input | Safe refusal without corruption | acceptance.md | accept |
"""


def _write_fidelity_judgment(root, body=FIDELITY_JUDGMENT):
    brief = root / "client-brief"
    brief.mkdir(parents=True, exist_ok=True)
    (brief / "fidelity-judgment.md").write_text(body, encoding="utf-8")


def _build_runtime_tree(runtime_root, project=PROJECT, files=DELIVERABLE_FILES, *, fidelity=True):
    """Synthesize the node's REAL nodes/<path>/ deliverable workspace on disk. Returns its path."""
    proj_dir = addressing.node_dir(NODE, runtime_root)
    for rel, content in files.items():
        target = proj_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if fidelity:
        _write_fidelity_judgment(proj_dir)
    brief = proj_dir / "client-brief"
    brief.mkdir(parents=True, exist_ok=True)
    (brief / "intent-spec.md").write_text(
        DEFAULT_INTENT_SPEC,
        encoding="utf-8",
    )
    return proj_dir


def _tree_snapshot(root):
    """Map of relative-path -> bytes for every file under ``root`` (for untouched-assertions)."""
    snapshot = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


# ===========================================================================
# Agent-write sentinel. The promote write must be attributable to harnessd (the
# control plane), NEVER to a jailed agent. Every agent — L1 included — is
# write-jailed to its /runtime/ node subtree; the destination is OUTSIDE every
# jail. We assert NO jailed-agent write touched the destination two ways:
#   (1) every WAL row the promote journals carries actor='harnessd' (the ledger
#       hard-codes actor='harnessd' for the single writer);
#   (2) the destination's parent is OUTSIDE /runtime/ (structurally un-reachable
#       by a write-jailed agent), and the only writer that produced its bytes is
#       the control-plane copy.
# ===========================================================================

def _wal_rows_for(node_address):
    return [r for r in ledger.load_wal() if r.get("node_address") == node_address]


# ===========================================================================
# 1. ACCEPT-GATED PROMOTE (filesystem-path) — the headline Done-test.
#    On a FAKE accepted project (real /runtime/ tree + intent-spec destination +
#    L1 accept), promote LANDS the deliverable AT the captured destination and the
#    binding shows deliverable_state=delivered with delivery_destination recording
#    the target. write_targets stays the in-jail source. The write is the control
#    plane's (actor='harnessd'), NOT an agent's.
# ===========================================================================

def test_accept_promote_lands_deliverable_at_destination(runtime, tmp_path):
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)

    # The destination is a REAL temp dir OUTSIDE /runtime/ (outside every write-jail).
    dest = tmp_path / "delivery-out" / "demo-widget"
    assert runtime not in dest.parents and dest != runtime, (
        "the delivery destination must be OUTSIDE /runtime/ (outside every agent's write-jail) — "
        "that is the whole point of the control-plane cross-jail promote"
    )

    binding, token = _binding(
        delivery_destination=str(dest),
        delivery_kind="filesystem-path",
    )
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    # The op reports success.
    assert getattr(result, "ok", result) , f"accept-gated promote should succeed; got {result!r}"

    # (a) The deliverable BYTES landed AT the captured destination (real on-disk copy-out).
    for rel, content in DELIVERABLE_FILES.items():
        landed = dest / rel
        assert landed.is_file(), f"deliverable file {rel!r} did not land at the destination {dest}"
        assert landed.read_text(encoding="utf-8") == content, (
            f"deliverable file {rel!r} landed with wrong bytes (copy-out corrupted/mismatched)"
        )
    # The distinctive marker proves the ACTUAL deliverable bytes were copied, not a placeholder.
    assert "PROMOTED-DELIVERABLE-MARKER-7f3a" in (dest / "src/widget.py").read_text("utf-8")

    # (b) The binding shows deliverable_state=delivered, recorded via the executor.
    after = _read()
    assert after["deliverable_state"] == "delivered", (
        f"expected deliverable_state=delivered after a successful promote, got "
        f"{after['deliverable_state']!r}"
    )

    # (c) delivery_destination records the target.
    assert after["delivery_destination"] == str(dest), (
        "delivery_destination must record the captured target after promote"
    )

    # (d) write_targets is NOT overloaded with the destination — it stays the in-jail source surface.
    assert after["write_targets"] == ["proj/demo-widget/"], (
        "write_targets must stay the IN-JAIL source surface — the out-of-jail destination belongs "
        "in delivery_destination, NEVER overloaded onto write_targets (DAEMON §3.2: distinct fields)"
    )
    assert str(dest) not in after["write_targets"], (
        "the out-of-jail destination leaked into write_targets — the jail boundary is no longer "
        "legible (the overload mutant)"
    )


def test_accept_promote_can_copy_explicit_delivery_source_surface(runtime, tmp_path):
    """The delivery source can be a product surface inside the node, not the whole node archive."""
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)
    product = proj_dir / "build" / "app"
    (product / "logview").mkdir(parents=True, exist_ok=True)
    (product / "pyproject.toml").write_text("[project]\nname = 'logview'\n", encoding="utf-8")
    (product / "logview" / "__main__.py").write_text("print('product root')\n", encoding="utf-8")
    (proj_dir / "brief.md").write_text("harness-side project brief\n", encoding="utf-8")
    dest = tmp_path / "delivery-out" / "logview"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE), delivery_source="build/app")

    assert result.ok is True and result.delivered is True, result.errors
    assert (dest / "pyproject.toml").is_file()
    assert (dest / "logview" / "__main__.py").is_file()
    assert not (dest / "build").exists(), "delivery_source should ship the selected surface as root"
    assert not (dest / "brief.md").exists(), "node-level harness/project scaffolding should not ship"
    assert not (dest / "client-brief" / "fidelity-judgment.md").exists()
    after = _read()
    assert after["delivery_source"] == "build/app"


def test_delivery_source_escape_is_delivery_failed(runtime, tmp_path):
    """A delivery source override must stay inside the promoted node workspace."""
    promote = _promote_callable()
    _build_runtime_tree(runtime)
    outside = tmp_path / "outside-product"
    outside.mkdir()
    (outside / "README.md").write_text("outside\n", encoding="utf-8")
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE), delivery_source=str(outside))

    assert result.ok is False and result.delivered is False
    assert any("delivery_source" in str(e) and "inside" in str(e) for e in result.errors)
    assert not dest.exists()
    assert _read()["deliverable_state"] == "delivery-failed"


def test_accept_without_fidelity_judgment_holds_promote_gate(runtime, tmp_path):
    """Increment 5: L1 accept alone is not the top fidelity gate. The promotion edge requires the
    L1-authored client-brief/fidelity-judgment.md artifact with an accept verdict; otherwise the
    gate holds and deliverable_state stays untouched."""
    promote = _promote_callable()
    _build_runtime_tree(runtime, fidelity=False)
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is False and result.delivered is False
    assert any("MISSING-FIDELITY-JUDGMENT" in str(e) for e in (result.errors or [])), (
        f"the held gate must name the missing fidelity judgment; got {result.errors!r}"
    )
    assert not dest.exists(), "promotion without the L1 fidelity artifact crossed the jail boundary"
    assert _read()["deliverable_state"] == "completed", (
        "a missing fidelity judgment is a held gate, not a delivery failure"
    )


def test_l1_preliminary_accept_without_owner_playback_confirmation_holds_gate(
    runtime, tmp_path
):
    """Q6: an L1 fidelity verdict is preliminary; it cannot cross the jail by itself."""
    promote = _promote_callable()
    _build_runtime_tree(runtime)
    dest = tmp_path / "owner-final-required"
    binding, _ = _binding(
        delivery_destination=str(dest),
        delivery_kind="filesystem-path",
    )
    _seed(binding, owner_confirmed=False)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is False
    assert result.delivered is False
    assert any(
        "OWNER-CONFIRMED-FIDELITY-PLAYBACK" in str(error)
        for error in result.errors
    )
    assert not dest.exists()


def test_reject_fidelity_judgment_holds_promote_gate(runtime, tmp_path):
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)
    _write_fidelity_judgment(
        proj_dir,
        FIDELITY_JUDGMENT.replace(
            "Preliminary Verdict: accept",
            "Preliminary Verdict: reject",
        ),
    )
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is False and result.delivered is False
    assert any("FIDELITY-JUDGMENT-NOT-ACCEPT" in str(e) for e in (result.errors or []))
    assert not dest.exists()
    assert _read()["deliverable_state"] == "completed"


def test_markdown_heading_fidelity_accept_verdict_is_accepted(runtime, tmp_path):
    """The preliminary L1 artifact may naturally use a markdown heading for the verdict line."""
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)
    _write_fidelity_judgment(
        proj_dir,
        FIDELITY_JUDGMENT.replace(
            "Preliminary Verdict: accept",
            "## Preliminary Verdict: **ACCEPT**",
        ),
    )
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is True and result.delivered is True, result.errors
    assert (dest / "README.md").is_file()


def test_fidelity_judgment_discovered_in_single_project_subtree(runtime, tmp_path):
    """Portfolio-shaped L1 nodes may carry the project client-brief under a child project dir."""
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime, fidelity=False)
    nested = proj_dir / "sitegen"
    _write_fidelity_judgment(nested)
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is True and result.delivered is True, result.errors
    assert (dest / "README.md").is_file()


def test_multiple_nested_fidelity_judgments_hold_promote_gate(runtime, tmp_path):
    """A root-addressed promote must not guess between multiple project-subtree L1 judgments."""
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime, fidelity=False)
    _write_fidelity_judgment(proj_dir / "sitegen")
    _write_fidelity_judgment(proj_dir / "api")
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is False and result.delivered is False
    assert any("AMBIGUOUS-FIDELITY-JUDGMENT" in str(e) for e in (result.errors or []))
    assert not dest.exists()
    assert _read()["deliverable_state"] == "completed"


# ===========================================================================
# 2. SINGLE-WRITER attribution — the promote's deliverable_state write goes through
#    executor.transition (the one mutation path), journaling a WAL row with
#    actor='harnessd' and advancing last_applied_seq. NO second mutation path; the
#    write is the CONTROL PLANE's, never an agent's.
# ===========================================================================

def test_promote_state_write_is_single_writer_harnessd(runtime, tmp_path):
    promote = _promote_callable()
    _build_runtime_tree(runtime)
    dest = tmp_path / "delivery-out" / "demo-widget"

    binding, token = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    before = _read()
    seq_before = before["last_applied_seq"]

    promote(NODE, accept_signal=_accept_signal(NODE))

    after = _read()

    # The state change went through the executor commit path (last_applied_seq advanced — the
    # intent-first watermark only moves on a real executor.commit; a raw write_binding side-channel
    # would NOT advance it).
    assert after["last_applied_seq"] > seq_before, (
        "deliverable_state must be written via the SINGLE writer (executor.transition/commit), "
        "which stamps last_applied_seq from the WAL entry — a second mutation path (raw "
        "write_binding) would not advance the watermark (DAEMON §4.4 intent-first)"
    )

    # A WAL row journals the delivery, attributable to harnessd (the single writer hard-codes
    # actor='harnessd'). No agent ever writes the ledger.
    rows = _wal_rows_for(NODE)
    delivery_rows = [
        r for r in rows
        if r.get("binding_delta", {}).get("deliverable_state") == "delivered"
        or "deliver" in (r.get("event", "") or "").lower()
        or r.get("to_state") == "delivered"
    ]
    assert delivery_rows, (
        "the promote must journal the delivery in the WAL (the single-writer audit log) — found "
        f"no delivery row among {[r.get('event') for r in rows]}"
    )
    assert all(r.get("actor") == "harnessd" for r in delivery_rows), (
        "every promote WAL row must be attributable to harnessd (the control plane / single "
        "writer), NEVER a jailed agent — actor is hard-coded to 'harnessd' by the ledger"
    )


# ===========================================================================
# 3. CONTROL-PLANE attribution at the destination — the destination sits OUTSIDE
#    /runtime/ (structurally un-reachable by any write-jailed agent), and its bytes
#    were produced by the control-plane copy. Assert NO jailed-agent write touched
#    the destination: the only writer that could reach outside /runtime/ is harnessd.
# ===========================================================================

def test_destination_is_outside_runtime_jail_and_written_by_control_plane(runtime, tmp_path):
    promote = _promote_callable()
    _build_runtime_tree(runtime)
    dest = tmp_path / "delivery-out" / "demo-widget"

    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    promote(NODE, accept_signal=_accept_signal(NODE))

    # The destination is genuinely outside the /runtime/ tree (no agent jail covers it).
    assert runtime not in dest.parents, (
        "the destination must be outside /runtime/ — a write-jailed agent (allow-list scoped to "
        "its node WORKROOT under /runtime/) is STRUCTURALLY unable to write here; only the "
        "control plane can cross this boundary (INTAKE-TO-DELIVERY §3)"
    )
    # The deliverable landed there nonetheless => the writer was NOT a jailed agent (it crossed a
    # boundary no jailed agent can cross). The control plane is the only candidate.
    assert (dest / "README.md").is_file(), (
        "deliverable did not land outside the jail — the control-plane cross-jail copy did not run"
    )


# ===========================================================================
# 4. REJECT PATH — with NO deliberate trigger, promote is a NO-OP: the destination is
#    untouched and /runtime/ is left intact. A speculative promote is caught here.
# ===========================================================================

def test_no_accept_is_noop_destination_untouched_runtime_intact(runtime, tmp_path):
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)

    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)
    before_runtime = _tree_snapshot(proj_dir)
    before_binding = _read()

    # NO delivery trigger at all (accept_signal=None) — the gate must hold.
    result = promote(NODE, accept_signal=None)

    # The op did not deliver.
    assert not getattr(result, "ok", False), (
        "promote with NO delivery trigger must NOT report a successful delivery (gated, never "
        "speculative)"
    )

    # The destination is UNTOUCHED — nothing was created there.
    assert not dest.exists(), (
        "promote with no accept wrote to the destination — a SPECULATIVE promote (the gate is "
        "missing: a wrong impl that promotes without an accept is caught here)"
    )

    # /runtime/ is left INTACT — byte-for-byte unchanged.
    assert _tree_snapshot(proj_dir) == before_runtime, (
        "the /runtime/ tree changed on a no-op promote — promotion must not mutate the source on "
        "a gated no-op"
    )

    # The binding's deliverable_state did NOT advance to delivered.
    after = _read()
    assert after["deliverable_state"] != "delivered", (
        "deliverable_state advanced to delivered on a no-accept promote — the gate did not hold"
    )
    assert after["delivery_destination"] == before_binding["delivery_destination"], (
        "delivery_destination changed on a no-op promote"
    )


def test_reject_signal_is_noop_destination_untouched(runtime, tmp_path):
    """A REJECT (not merely a missing accept) is also a no-op — the gate is on ACCEPT, not presence."""
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)

    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)
    before_runtime = _tree_snapshot(proj_dir)

    result = promote(NODE, accept_signal=_reject_signal(NODE))

    assert not getattr(result, "ok", False), (
        "a REJECT signal must NOT deliver — the gate proceeds only on an ACCEPT decision"
    )
    assert not dest.exists(), (
        "promote on a REJECT wrote to the destination — a reject is a no-op, not a delivery"
    )
    assert _tree_snapshot(proj_dir) == before_runtime, "/runtime/ mutated on a reject no-op"
    assert _read()["deliverable_state"] != "delivered", (
        "deliverable_state advanced to delivered on a REJECT — the accept gate did not hold"
    )


# ===========================================================================
# 5. FAILED PROMOTE — a copy-out failure sets deliverable_state=delivery-failed and
#    ESCALATES (the §6.3 run-ledger escalation seam). A silent success on failure is
#    caught here.
#
#    We force a real failure by pointing delivery_destination at a path whose parent
#    is a REGULAR FILE — the OS cannot create a directory child of a file, so the
#    real copy-out raises. No mock; a genuine filesystem failure.
# ===========================================================================

def test_failed_promote_sets_delivery_failed_and_escalates(runtime, tmp_path):
    promote = _promote_callable()
    _build_runtime_tree(runtime)

    # A regular file standing where the destination's PARENT directory must be — the copy-out
    # cannot create a directory under a file, so the real filesystem write fails.
    blocker = tmp_path / "blocker-file"
    blocker.write_text("i am a file, not a directory\n", encoding="utf-8")
    dest = blocker / "demo-widget"  # parent (blocker) is a file => mkdir/copy must fail

    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    # The op does NOT report a successful delivery.
    assert not getattr(result, "ok", True) if hasattr(result, "ok") else True, (
        "a failed promote must not report ok=True (no silent success on failure)"
    )

    # deliverable_state=delivery-failed on the binding (the §3.2 failure value).
    after = _read()
    assert after["deliverable_state"] == "delivery-failed", (
        "a failed copy-out must set deliverable_state=delivery-failed (DAEMON §3.2) — NOT delivered, "
        f"NOT left unchanged; got {after['deliverable_state']!r} (the silent-success-on-failure mutant)"
    )

    # An escalation is journaled (the §6.3 run-ledger escalation seam — chokepoint precedent: a
    # failure emits an L1-readable escalation WAL row). We look for an escalation/failure row that
    # names this node.
    rows = _wal_rows_for(NODE)
    escalation_rows = [
        r for r in rows
        if "escal" in (r.get("event", "") or "").lower()
        or "fail" in (r.get("event", "") or "").lower()
        or r.get("binding_delta", {}).get("deliverable_state") == "delivery-failed"
    ]
    assert escalation_rows, (
        "a failed promote must ESCALATE — journal an L1-readable escalation/delivery-failed row in "
        f"the run-ledger (the §6.3 seam). Found events: {[r.get('event') for r in rows]}"
    )
    assert all(r.get("actor") == "harnessd" for r in escalation_rows), (
        "the escalation row must be attributable to harnessd (the control plane)"
    )
    failed_targets = after.get("delivery_failed_targets") or []
    assert failed_targets and failed_targets[-1]["destination"] == str(dest)
    assert "file" in failed_targets[-1]["reason"].lower()


def test_delivery_failed_can_retry_with_explicit_destination_override(runtime, tmp_path):
    """T48 repair path: after a bad promote, the control plane can retry to an explicit target
    without editing the frozen intent-spec or live binding by hand."""
    promote = _promote_callable()
    _build_runtime_tree(runtime)
    blocker = tmp_path / "blocker-file-retry"
    blocker.write_text("file-not-dir\n", encoding="utf-8")
    stale_dest = blocker / "demo-widget"
    good_dest = tmp_path / "delivery-out" / "demo-widget"

    binding, _ = _binding(delivery_destination=str(stale_dest), delivery_kind="filesystem-path")
    _seed(binding)

    first = promote(NODE, accept_signal=_accept_signal(NODE))

    assert first.ok is False
    assert _read()["deliverable_state"] == "delivery-failed"

    retry = promote(
        NODE,
        accept_signal=_accept_signal(NODE),
        delivery_destination_override=str(good_dest),
    )

    assert retry.ok is True and retry.delivered is True, retry.errors
    assert (good_dest / "README.md").is_file()
    after = _read()
    assert after["deliverable_state"] == "delivered"
    assert after["delivery_destination"] == str(good_dest)
    failed_targets = after.get("delivery_failed_targets") or []
    assert failed_targets and failed_targets[-1]["destination"] == str(stale_dest)


def test_delivery_destination_override_requires_prior_delivery_failure(runtime, tmp_path):
    """A retry destination is a repair path, not a way to bypass the frozen §8 destination."""
    promote = _promote_callable()
    _build_runtime_tree(runtime)
    original_dest = tmp_path / "original" / "demo-widget"
    override_dest = tmp_path / "override" / "demo-widget"

    binding, _ = _binding(delivery_destination=str(original_dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(
        NODE,
        accept_signal=_accept_signal(NODE),
        delivery_destination_override=str(override_dest),
    )

    assert result.ok is False and result.delivered is False
    assert any("DELIVERY-RETRY-REQUIRES-FAILED" in str(e) for e in result.errors)
    assert not original_dest.exists()
    assert not override_dest.exists()
    assert _read()["deliverable_state"] == "completed"


def test_failed_promote_leaves_write_targets_in_jail_source(runtime, tmp_path):
    """Even on failure, write_targets stays the in-jail source surface (never overloaded)."""
    promote = _promote_callable()
    _build_runtime_tree(runtime)
    blocker = tmp_path / "blocker-file2"
    blocker.write_text("file-not-dir\n", encoding="utf-8")
    dest = blocker / "demo-widget"

    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    promote(NODE, accept_signal=_accept_signal(NODE))

    after = _read()
    assert after["write_targets"] == ["proj/demo-widget/"], (
        "write_targets must remain the in-jail source surface even on a failed promote"
    )


# ===========================================================================
# 6. GIT-REMOTE VARIANT — delivery_kind=git-remote. The promote PUSHES the
#    deliverable to a REAL local bare git repo (real `git init --bare` + real
#    `git push`), same accept gate. We assert the deliverable bytes are retrievable
#    from the bare remote (the push genuinely landed) and the binding shows
#    deliverable_state=delivered with delivery_destination recording the remote.
# ===========================================================================

def _git(args, cwd, check=True):
    env = {
        "GIT_AUTHOR_NAME": "harnessd",
        "GIT_AUTHOR_EMAIL": "harnessd@example.invalid",
        "GIT_COMMITTER_NAME": "harnessd",
        "GIT_COMMITTER_EMAIL": "harnessd@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(cwd),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, check=check,
    )


def test_git_remote_promote_pushes_to_real_bare_remote(runtime, tmp_path):
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)

    # The /runtime/ deliverable tree must be a real git repo with a commit for `git push` to have
    # something to push (the build produced the tree; here we make it a committed work tree).
    _git(["init", "-b", "main"], cwd=proj_dir)
    _git(["add", "-A"], cwd=proj_dir)
    _git(["commit", "-m", "finished deliverable"], cwd=proj_dir)

    # A REAL local bare repo standing in for the captured git remote (outside /runtime/).
    bare = tmp_path / "delivery-remote.git"
    _git(["init", "--bare", str(bare)], cwd=tmp_path)

    binding, _ = _binding(delivery_destination=str(bare), delivery_kind="git-remote")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))
    assert getattr(result, "ok", result), f"git-remote promote should succeed; got {result!r}"

    # The push genuinely landed: clone the bare remote and assert the deliverable bytes are there.
    checkout = tmp_path / "verify-clone"
    clone = _git(["clone", str(bare), str(checkout)], cwd=tmp_path, check=False)
    assert clone.returncode == 0, (
        f"could not clone the delivery remote — the push did not land. stderr: {clone.stderr}"
    )
    landed = checkout / "src" / "widget.py"
    assert landed.is_file(), "deliverable was not pushed to the bare remote"
    assert "PROMOTED-DELIVERABLE-MARKER-7f3a" in landed.read_text("utf-8"), (
        "the pushed bytes are not the actual deliverable (push did not carry the real tree)"
    )

    # The binding records delivered + the remote, write_targets untouched.
    after = _read()
    assert after["deliverable_state"] == "delivered", (
        f"git-remote promote must set deliverable_state=delivered; got {after['deliverable_state']!r}"
    )
    assert after["delivery_destination"] == str(bare), (
        "delivery_destination must record the captured git remote"
    )
    assert after["write_targets"] == ["proj/demo-widget/"], (
        "write_targets must stay the in-jail source surface for the git-remote variant too"
    )


def test_git_remote_no_accept_is_noop_remote_empty(runtime, tmp_path):
    """The git-remote gate: with no accept, nothing is pushed to the bare remote (empty remote)."""
    promote = _promote_callable()
    proj_dir = _build_runtime_tree(runtime)
    _git(["init", "-b", "main"], cwd=proj_dir)
    _git(["add", "-A"], cwd=proj_dir)
    _git(["commit", "-m", "finished deliverable"], cwd=proj_dir)

    bare = tmp_path / "delivery-remote-empty.git"
    _git(["init", "--bare", str(bare)], cwd=tmp_path)

    binding, _ = _binding(delivery_destination=str(bare), delivery_kind="git-remote")
    _seed(binding)

    promote(NODE, accept_signal=None)

    # The bare remote has NO refs (nothing was pushed — the gate held).
    refs = _git(["for-each-ref"], cwd=bare, check=False)
    assert refs.stdout.strip() == "", (
        "a no-accept git-remote promote pushed to the remote — speculative push (gate missing)"
    )
    assert _read()["deliverable_state"] != "delivered", (
        "deliverable_state advanced to delivered on a no-accept git-remote promote"
    )


# ===========================================================================
# 7. THE LEGACY-PATH MUTATION KILL (F8 / JSF-03) — promote must NOT read the
#    stale pre-FORK-NODE-NESTING /runtime/proj/{project}/ layout. The deliverable
#    tree is built ONLY at the LEGACY location (the canonical nodes/<path>/
#    workspace absent): a correct impl finds NO source and routes to the journaled
#    delivery-failed + §6.3 escalation path; an impl that still (or again) reads
#    /runtime/proj/ would deliver from a path no agent ever writes.
# ===========================================================================

def test_promote_does_not_read_the_legacy_proj_layout(runtime, tmp_path):
    promote = _promote_callable()

    # Build the deliverable ONLY at the LEGACY flat location — no nodes/<path>/ tree exists.
    legacy = runtime / "proj" / PROJECT
    for rel, content in DELIVERABLE_FILES.items():
        target = legacy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    assert not addressing.node_dir(NODE, runtime).exists(), (
        "precondition: the canonical nodes/<path>/ workspace must be ABSENT for this kill to bite"
    )

    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _ = _binding(delivery_destination=str(dest), delivery_kind="filesystem-path")
    _seed(binding)

    result = promote(NODE, accept_signal=_accept_signal(NODE))

    # The op did NOT deliver — the legacy location is dead, never read.
    assert not getattr(result, "ok", True), (
        "promote reported success with the deliverable ONLY at the legacy /runtime/proj/ location — "
        "it is reading a path no agent ever writes (the revert-to-/runtime/proj/ mutant)"
    )
    assert not dest.exists(), (
        "bytes landed at the destination from the LEGACY /runtime/proj/ tree — promote must source "
        "the canonical nodes/<path>/ workspace (addressing.node_dir) only"
    )

    # The absent canonical source is a JOURNALED delivery-failed (+ the §6.3 escalation row).
    after = _read()
    assert after["deliverable_state"] == "delivery-failed", (
        "an absent canonical source tree must be a journaled delivery-failed, got "
        f"{after['deliverable_state']!r}"
    )
    rows = _wal_rows_for(NODE)
    escalation_rows = [r for r in rows if r.get("event") == "delivery_failed_escalation"]
    assert escalation_rows, (
        "an absent-source promote must emit the §6.3 'delivery_failed_escalation' WAL row — found "
        f"events: {[r.get('event') for r in rows]}"
    )
    assert all(r.get("actor") == "harnessd" for r in escalation_rows), (
        "the escalation row must be attributable to harnessd (the control plane)"
    )


# ===========================================================================
# E3 — the PROMOTE GATE half of the enforcement spine (2026-06-11).
#
# (a) DERIVATION: the §8 delivery destination is read from the node's frozen
#     intent-spec (client-brief/intent-spec.md) when the binding lacks it —
#     INTAKE-TO-DELIVERY §3 / intent-spec-contract §8; an EXPLICIT `in-place`
#     marking is the sanctioned no-external-delivery (the deliverable stays in
#     the node; promote stamps delivered without a cross-jail copy).
# (b) FREEZE-ON-PENDING: an intent-spec carrying a load-bearing requirement row
#     still `pending` reflect-back BLOCKS accept (PLAN-ALIGNMENT-GATE: "the gate
#     refuses to let anything be built on or frozen against an unconfirmed
#     foundation") — the user-authority forcing function at the delivery edge.
# ===========================================================================

def _write_intent_spec(runtime_root, body, node_address=NODE):
    d = addressing.node_dir(node_address, runtime_root) / "client-brief"
    d.mkdir(parents=True, exist_ok=True)
    (d / "intent-spec.md").write_text(body, encoding="utf-8")
    if ledger.read_binding(node_address) is not None:
        _confirm_current_playback(node_address)


_SPEC_IN_PLACE = """# intent-spec — demo widget
## Outcomes
| Outcome ID | Outcome |
|---|---|
| O-001 | The recipient can run the demo widget. |
## Requirements
| ID | Requirement | Tag | MNF | Reflect-back status |
|---|---|---|---|---|
| R-001 | render the widget | decided | YES | confirmed |
## 8. Delivery destination
| Destination | in-place / no external delivery |
| Kind | in-place |
"""

_SPEC_PENDING = """# intent-spec — demo widget
## Outcomes
| Outcome ID | Outcome |
|---|---|
| O-001 | The recipient can run the demo widget. |
## Requirements
| ID | Requirement | Tag | MNF | Reflect-back status |
|---|---|---|---|---|
| R-001 | render the widget | decided | YES | pending |
## 8. Delivery destination
| Destination | in-place / no external delivery |
| Kind | in-place |
"""


def _spec_filesystem_destination(destination):
    return f"""# intent-spec — demo widget
## Outcomes
| Outcome ID | Outcome |
|---|---|
| O-001 | The recipient can run the demo widget. |
## Requirements
| ID | Requirement | Tag | MNF | Reflect-back status |
|---|---|---|---|---|
| R-001 | render the widget | decided | YES | confirmed |
## 8. Delivery destination
| Destination | {destination} |
| Kind | filesystem-path |
"""


def test_e3_accept_derives_in_place_destination_from_intent_spec(runtime):
    """Binding lacks delivery_destination; the frozen intent-spec marks §8 in-place -> promote
    accept DERIVES it, skips the cross-jail copy, and stamps delivered/in-place.
    (Mutant: no derivation -> ValueError no-destination -> delivery-failed -> caught.)"""
    promote_mod = _promote_module()
    _build_runtime_tree(runtime)
    binding, _token = _binding(delivery_destination=None, delivery_kind=None)
    _seed(binding)
    _write_intent_spec(runtime, _SPEC_IN_PLACE)

    result = promote_mod.promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is True and result.delivered is True, (
        f"an explicit in-place §8 must promote (errors: {getattr(result, 'errors', None)})"
    )
    after = _read()
    assert after["deliverable_state"] == "delivered"
    assert "in-place" in (after.get("delivery_destination") or ""), (
        "the derived in-place destination must be stamped on the binding"
    )


def test_e3_markdown_destination_prefers_absolute_annotation_and_normalizes(runtime, tmp_path):
    """A human-facing destination row may carry a home shorthand plus the expanded absolute path.
    Promote must extract the real filesystem target, not treat the whole markdown cell as a path."""
    promote_mod = _promote_module()
    _build_runtime_tree(runtime)
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _token = _binding(delivery_destination=None, delivery_kind=None)
    _seed(binding)
    _write_intent_spec(runtime, _spec_filesystem_destination(f"`~/Projects/logview` (`{dest}`)"))

    result = promote_mod.promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is True and result.delivered is True, result.errors
    assert (dest / "README.md").is_file(), (
        "the expanded absolute destination from the spec annotation should receive the deliverable"
    )
    after = _read()
    assert after["delivery_destination"] == str(dest), (
        "the binding must record the normalized filesystem path, not the markdown table cell"
    )


def test_e3_intent_spec_destination_overrides_stale_binding_destination(runtime, tmp_path):
    """The frozen intent-spec is authoritative at promote time. A stale cached destination on the
    binding must not trap a corrected delivery row into repeatedly delivering to the old target."""
    promote_mod = _promote_module()
    _build_runtime_tree(runtime)
    stale = tmp_path / "stale-cache"
    dest = tmp_path / "delivery-out" / "demo-widget"
    binding, _token = _binding(delivery_destination=str(stale), delivery_kind="filesystem-path")
    _seed(binding)
    _write_intent_spec(runtime, _spec_filesystem_destination(f"`{dest}`"))

    result = promote_mod.promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is True and result.delivered is True, result.errors
    assert (dest / "README.md").is_file(), "the corrected spec destination should receive delivery"
    assert not stale.exists(), "the stale cached binding destination should not receive delivery"
    assert _read()["delivery_destination"] == str(dest)


def test_e3_pending_reflect_back_blocks_accept(runtime):
    """FREEZE-ON-PENDING: a load-bearing requirement row still `pending` reflect-back REFUSES the
    accept-promote — deliverable_state untouched, nothing crosses the jail, the error NAMES the
    block. (Mutant: promote ignores the intent-spec -> delivers on an unconfirmed foundation.)"""
    promote_mod = _promote_module()
    _build_runtime_tree(runtime)
    binding, _token = _binding(delivery_destination=None, delivery_kind=None)
    _seed(binding)
    _write_intent_spec(runtime, _SPEC_PENDING)

    result = promote_mod.promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is False and result.delivered is False
    assert any("FREEZE-ON-PENDING" in str(e) for e in (result.errors or [])), (
        f"the refusal must NAME the freeze block; got {result.errors!r}"
    )
    after = _read()
    assert after["deliverable_state"] == "completed", (
        "a freeze-blocked accept must leave the deliverable state UNTOUCHED (no delivery-failed "
        "stamp — the artifact is unconfirmed, not the delivery broken)"
    )


def test_e3_binding_destination_still_respects_freeze_block(runtime):
    """Even with a binding-stamped destination, a pending load-bearing row blocks accept — the
    freeze block reads the ARTIFACT, not the binding."""
    promote_mod = _promote_module()
    _build_runtime_tree(runtime)
    binding, _token = _binding(delivery_destination="/tmp/never-used-e3", delivery_kind="filesystem-path")
    _seed(binding)
    _write_intent_spec(runtime, _SPEC_PENDING)

    result = promote_mod.promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is False and result.delivered is False
    assert any("FREEZE-ON-PENDING" in str(e) for e in (result.errors or []))


def test_e3_discovers_the_intent_spec_in_a_project_subtree(runtime):
    """LR-22: WORKSPACE-SCHEMA puts client-brief/ under project-{name}/ INSIDE the L1 node — the
    portfolio shape both live runs produced (L1/wordcount/, L1/sitegen/). Discovery limited to the
    node root missed it and the live Run-2 promote refused a fully-confirmed in-place delivery.
    A single project subtree's frozen spec must carry the derivation. (Mutant: root-only
    discovery -> no-destination refusal -> caught.)"""
    promote_mod = _promote_module()
    _build_runtime_tree(runtime)
    binding, _token = _binding(delivery_destination=None, delivery_kind=None)
    _seed(binding)
    (
        addressing.node_dir(NODE, runtime)
        / "client-brief"
        / "intent-spec.md"
    ).unlink()
    d = addressing.node_dir(NODE, runtime) / "sitegen" / "client-brief"
    d.mkdir(parents=True, exist_ok=True)
    (d / "intent-spec.md").write_text(_SPEC_IN_PLACE, encoding="utf-8")
    _confirm_current_playback()

    result = promote_mod.promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is True and result.delivered is True, (
        f"a project-subtree §8 in-place spec must promote (LR-22); errors: {result.errors!r}"
    )
    after = _read()
    assert after["deliverable_state"] == "delivered"
    assert "in-place" in (after.get("delivery_destination") or "")


def test_e3_refuses_an_ambiguous_multi_project_portfolio(runtime):
    """LR-22 ambiguity leg: TWO project subtrees each carrying a frozen intent-spec make the §8
    derivation ambiguous for a root-addressed promote — refuse LOUDLY, naming both candidates;
    never guess a destination. (Mutant: pick-first -> silent wrong-project delivery -> caught.)"""
    promote_mod = _promote_module()
    _build_runtime_tree(runtime)
    binding, _token = _binding(delivery_destination=None, delivery_kind=None)
    _seed(binding)
    (
        addressing.node_dir(NODE, runtime)
        / "client-brief"
        / "intent-spec.md"
    ).unlink()
    for proj in ("alpha", "beta"):
        d = addressing.node_dir(NODE, runtime) / proj / "client-brief"
        d.mkdir(parents=True, exist_ok=True)
        (d / "intent-spec.md").write_text(_SPEC_IN_PLACE, encoding="utf-8")

    result = promote_mod.promote(NODE, accept_signal=_accept_signal(NODE))

    assert result.ok is False and result.delivered is False
    joined = " ".join(str(e) for e in (result.errors or []))
    assert "alpha" in joined and "beta" in joined, (
        f"the ambiguity refusal must NAME the candidate project specs; got {result.errors!r}"
    )
    after = _read()
    assert after["deliverable_state"] != "delivered"
