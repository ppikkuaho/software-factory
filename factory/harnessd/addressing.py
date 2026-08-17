"""addressing — the ONE canonical address→workspace mapping (the one-spine, NESTED).

An address is ``<path>#<seat>`` (e.g. ``proj/payments/gateway/stripe-client#exec``). The node WORKSPACE
is the PATH, nested on disk: ``<RUNTIME_ROOT>/nodes/proj/payments/gateway/stripe-client/``. Nesting is
load-bearing: a child's dir sits UNDER its parent's, so the write-jail's existing "write your WORKROOT
subtree" rule gives a coordinator write access to its children's nodes — which is exactly the canonical
write model:

  * ARCHITECTURE.md:122 — "Write access follows the NESTED tree — each level writes within its own
    workspace and CREATES CHILD WORKSPACES WITHIN IT."
  * IMPLEMENTATION-PLAN — "every agent is write-jailed to its /runtime/ node SUBTREE."
  * WORKSPACE-SCHEMA — the visibility graph: own subtree + same-level siblings + parent.

The earlier flat ``a/b#seat -> a-b-seat`` collapse (a misread of the plan-diagram word "collapsed-
address" as flatten-slashes, when the spec means collapse-the-#seat-but-keep-the-path) put every node
in its OWN flat dir, so a coordinator's WORKROOT did NOT contain its children — which is why the build
fell back to passing the brief as spawn payload. This module is the fix: ONE nested derivation, used by
the chokepoint (node/brief landing), the sandbox (jail WORKROOT), the detector (sign-off signal), and
the watchdog (wake inbox), so they cannot drift.

THE SEAT is NOT a path segment. ``#exec`` and ``#review`` are two ACTORS on ONE node (the reviewer reads
the executor's work in place), so they share the node WORKSPACE. Making the seat a trailing path segment
would break child-nesting (a child of ``parser#exec`` would not sit under ``parser/exec/``). Only the
PER-ACTOR metadata files — the sign-off ``.signal`` and the wake ``.inbox`` — are seat-qualified, so the
L5/L5+ pair sharing a node dir don't clobber each other's sign-off.
"""

from pathlib import Path

# The default seat when an address carries no ``#`` (the working incarnation).
DEFAULT_SEAT: str = "exec"

# The runtime subdir that holds all node workspaces (``<RUNTIME_ROOT>/nodes/<nested-path>/``).
NODES_DIRNAME: str = "nodes"
# The sender-owned communication surface inside one node workspace.
MESSAGES_DIRNAME: str = "messages"


def split_address(address: str) -> tuple[str, str]:
    """``a/b#seat`` -> ``("a/b", "seat")``. No ``#`` -> the default ``exec`` seat.

    The PATH is what nests on disk; the SEAT distinguishes two actors on the same node.
    """
    path, sep, seat = address.partition("#")
    return path, (seat if (sep and seat) else DEFAULT_SEAT)


def node_path(address: str) -> str:
    """The nested on-disk relpath for a node — the PATH part only (the ``#seat`` is NOT a segment)."""
    return split_address(address)[0]


def node_dir(address: str, runtime_root) -> Path:
    """``<runtime_root>/nodes/<nested-path>/`` — a child's dir nests UNDER its parent's (subtree).

    This is BOTH the agent's workspace and the jail WORKROOT, so ``(allow file-write* (subpath
    WORKROOT))`` on a coordinator's node dir covers all of its descendants' dirs.
    """
    return Path(runtime_root) / NODES_DIRNAME / node_path(address)


def signal_path(address: str, runtime_root) -> Path:
    """The per-SEAT sign-off signal file: ``<node-dir>/.signal.<seat>.json``.

    Seat-qualified so the L5 (exec) and L5+ (review) sharing one node dir don't clobber each other's
    terminal signal (each actor signs off independently).
    """
    _, seat = split_address(address)
    return node_dir(address, runtime_root) / f".signal.{seat}.json"


def signoff_path(address: str, runtime_root) -> Path:
    """The per-SEAT sign-off HANDSHAKE file: ``<node-dir>/.sign-off.<seat>.json`` (F19).

    The chokepoint seeds it strictly AFTER the claim commits (it carries the POST-claim re-minted
    ``owner_token``) and strictly BEFORE the actor opens — the only agent-visible channel for the
    token the ``signal_path`` fence validates (the brief payload omits it, brief.md can be
    pre-authored before the claim mints the token, and the pane env is contractually the 4
    isolation vars). The agent copies ``owner_token`` verbatim into its ``.signal.<seat>.json``.
    Seat-qualified like ``signal_path`` so the L5/L5+ pair sharing one node dir don't clobber.
    """
    _, seat = split_address(address)
    return node_dir(address, runtime_root) / f".sign-off.{seat}.json"


def turn_state_path(address: str, runtime_root) -> Path:
    """The per-SEAT atomic current turn-state snapshot."""
    _, seat = split_address(address)
    return node_dir(address, runtime_root) / f".turn-state.{seat}.json"


def turn_events_path(address: str, runtime_root) -> Path:
    """The per-SEAT append-only observed turn-event log."""
    _, seat = split_address(address)
    return node_dir(address, runtime_root) / f".turn-events.{seat}.jsonl"


def owed_checklist_path(address: str, runtime_root) -> Path:
    """The per-SEAT live deterministic owed-product checklist."""
    _, seat = split_address(address)
    return node_dir(address, runtime_root) / f".owed-checklist.{seat}.json"


def turn_state_lock_path(address: str, runtime_root) -> Path:
    """The per-SEAT lock serializing concurrent runtime hook callbacks."""
    _, seat = split_address(address)
    return node_dir(address, runtime_root) / f".turn-state.{seat}.lock"


def inbox_path(address: str, runtime_root) -> Path:
    """The per-SEAT wake inbox: ``<node-dir>/.inbox.<seat>.jsonl`` (③-wake surface, multi-writer)."""
    _, seat = split_address(address)
    return node_dir(address, runtime_root) / f".inbox.{seat}.jsonl"


def messages_dir(address: str, runtime_root) -> Path:
    """The sender-owned durable message directory.

    The directory is shared by seats on the node workspace, while every canonical record still
    carries the full sender address. Marker ids are sender-binding scoped, so two seats cannot
    alias a record merely by sharing this directory.
    """
    return node_dir(address, runtime_root) / MESSAGES_DIRNAME


def message_marker_path(address: str, message_id: str, runtime_root) -> Path:
    """The canonical agent-written ``messages/<message-id>.json`` marker."""
    return messages_dir(address, runtime_root) / f"{message_id}.json"


def contract_rebind_dir(address: str, runtime_root) -> Path:
    """The holder-owned durable contract receipt rebind directory."""
    return node_dir(address, runtime_root) / "contract-rebind"


def contract_revision_dir(address: str, runtime_root) -> Path:
    """The contract-owner's immutable revision-record directory."""
    return node_dir(address, runtime_root) / "contract-revisions"


# ---------------------------------------------------------------------------
# THE ONE tmux session-name derivation (F18 / finding OSA-01).
#
# tmux 3.6a SILENTLY RENAMES session names containing ':' or '.' to '_' variants (probed live:
# 'harness:L1-exec' -> 'harness_L1-exec'; 'harness-proj-1.2-exec' -> 'harness-proj-1_2-exec'),
# so a recorded name carrying either char NEVER matches the live session. The canonical name
# therefore folds the WHOLE unsafe alphabet — '/', '#', ':', '.' — to '-', and the prefix is
# 'harness-' (NOT 'harness:'), a name tmux does not rewrite. Every session-name producer (the
# Claude adapter, the chokepoint child register, the genesis L1 register) derives through THIS
# one function so the ledger key and the live tmux key cannot drift.
#
# NOTE: this is the SESSION name only. The authoritative recorded ``tmux_target`` is the full
# '<session>:<window>.<pane>' triple tmux itself reports from ``create_detached`` (the
# post-rename truth + the REAL indices) — see harnessd/spawn/tmux.py.
# ---------------------------------------------------------------------------

# The operator-predictable session prefix: `tmux -L <socket> attach -t harness-<collapsed-address>`.
SESSION_PREFIX: str = "harness-"

# The characters tmux rewrites (':' '.') plus the address separators ('/' '#'), all folded to '-'.
_SESSION_UNSAFE = str.maketrans({"/": "-", "#": "-", ":": "-", ".": "-"})


def session_name_for(address: str) -> str:
    """``a/b.c#seat`` -> ``harness-a-b-c-seat`` — the ONE canonical tmux session name (F18).

    Folds '/', '#', ':' and '.' to '-' so tmux never silently renames the session, and prefixes
    ``harness-`` so the operator can predict the attach target. All session-name producers and
    the reconcile/detector match key derive through this single function (OSA-01: a second
    collapse implementation is exactly how the recorded and live keys drifted apart).
    """
    return SESSION_PREFIX + address.translate(_SESSION_UNSAFE)
