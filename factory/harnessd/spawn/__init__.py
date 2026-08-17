"""harnessd.spawn — the spawn chokepoint subpackage (IMPLEMENTATION-PLAN §2.11).

Increment 8 ships only the OAuth-only enforcer (``oauth_guard``), the HARD
INVARIANT that no part of the system may ever use a raw API key. Later
increments add the adapters, tmux transport, chokepoint, and brief modules
under this same package.

This is a REAL (regular) package — a concrete ``__init__.py``, not an implicit
namespace dir — so ``harnessd.spawn`` resolves with a real ``__spec__.origin``
(asserted by the Increment-8 acceptance suite).
"""

__all__ = ["oauth_guard"]
