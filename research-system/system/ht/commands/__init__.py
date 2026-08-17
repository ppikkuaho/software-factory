"""ht command implementations (one module per command group).

Each command builds an in-memory mutation and returns a pipeline.Plan; the CLI
runs it through the shared enforcement pipeline. Read-only commands (validate)
act directly.
"""

from . import phase

__all__ = ["phase"]
