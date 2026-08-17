"""ht — the hypothesis-tree research-state write tool (Phase 0 v0).

The ONLY sanctioned API for state mutation (A2 §4, concept D7). Every mutation
runs the same enforcement pipeline (schema -> role x field authority -> semantic
rules -> write -> wholesale index regen -> role-stamped git commit), and the
pre-commit hook (ht.hook) is a thin backstop calling the same authority/schema
logic so nothing is duplicated.
"""

__all__ = ["__version__"]

__version__ = "0.0.0"
