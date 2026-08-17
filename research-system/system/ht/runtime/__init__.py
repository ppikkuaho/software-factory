"""Independent hypothesis-tree runtime substrate.

The package owns only ``<research-root>/var/runtime``.  It deliberately has no
dependency on agent-bus, tmux, a model adapter, or the L1--L5 daemon.
"""

from __future__ import annotations

from pathlib import Path


SCHEMA_VERSION = "hypothesis-tree-runtime/1.0.0"
BUILD_ID = "ht-runtime-kernel/1.0.0"
RUNTIME_KIND = "hypothesis-tree"
FIXED_ROLE = "synthetic-kernel-v1"

# Re-exported after constants so process code has one stable public derivation
# API without reaching into the reducer's private implementation.
from .state import derive_dedup_key  # noqa: E402


def runtime_root(repository_root: Path) -> Path:
    """Return the one non-configurable runtime location for a research root."""

    return repository_root / "var" / "runtime"
