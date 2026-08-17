"""harnessd — the L1–L5 agent-harness daemon (walking skeleton).

Increment 0 ships the repo skeleton plus the two config-time seats the rest of
the code must NOT hardcode:

  * ``harnessd.states``  — the static legality table for the generic per-node
    lifecycle state machine (DAEMON §3.3) plus the ``is_terminal`` / ``is_legal``
    predicates (IMPLEMENTATION-PLAN §2.3 frozen interface).
  * ``harnessd.config``  — ``LevelConfig`` per level (model / runtime /
    role_variant / tool_manifest), the CONSTANT shared ``system_prompt_file``,
    the ``W(state)`` suspicion-window placeholder constants (FORK-W), and the
    pinned-binary version/hash seat (PINNED-CC).

Later-increment modules (ledger, executor, daemon, …) are intentionally NOT
imported here — Increment 0 is the package skeleton + these two modules only.
"""

__all__ = ["states", "config"]

__version__ = "0.0.0"
