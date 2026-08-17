"""harnessd.spawn.adapters — the hexagonal RuntimeAdapter port + its concrete fills.

The runtime-NEUTRAL spawn contract (``base.RuntimeAdapter.pin_and_open``) and the two v1
fills:

  * ``claude_code.ClaudeCodeAdapter`` — the concrete Claude-Code boot recipe (§2.11 / DAEMON
    §6.2 H40): the SHARED ``--system-prompt-file``, the 4-var isolation env, the from-empty
    ``env -i`` pane, the OAuth-only gate BEFORE ``create_detached``, and the recorded
    model_used / role_variant / system_prompt_file(_hash) / transcript_path facts.
  * ``codex.CodexAdapter`` — the UNDERSPECIFIED Codex port: a stub that raises a deterministic
    "adapter port to be supplied" (NOT a silent fallback) and still asserts the shared
    OAuth-only negative invariant (no OPENAI_API_KEY) so the unbuilt fill cannot delete the gate.
"""

__all__ = ["base", "claude_code", "codex"]
