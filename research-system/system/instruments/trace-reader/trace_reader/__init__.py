"""Trace reader — Stage 1 extractor for the hypothesis-tree research system (C6).

Deterministic, no-LLM. Turns a Claude Code session bundle or Codex rollout into a
normalized, typed event stream with anchor pointers back to exact source rows.
Claude is parent-chain aware; Codex is guarded by its observed lived-order
tripwire and never fabricates branch membership.
"""

EXTRACTOR_VERSION = "trace-reader/1.1.0"

__all__ = ["EXTRACTOR_VERSION"]
