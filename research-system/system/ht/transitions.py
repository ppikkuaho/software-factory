"""Status-transition legality graph (A1 §2.1; B4 §9).

  unexplored -> worked (via adjudication only), closed
  worked     -> parked, closed, merged
  parked     -> closed, worked   (ONLY via settle)
  closed     -> (terminal)
  merged     -> (terminal)

Explicitly illegal: unexplored -> merged; any exit from parked not through settle;
any write out of a terminal state. The graph is consulted by the node lifecycle
commands; each command additionally names WHICH transition it is allowed to make.
"""

from __future__ import annotations

from .errors import HtError

# from -> set(legal to)
LEGAL: dict[str, set[str]] = {
    "unexplored": {"worked", "closed"},
    "worked": {"parked", "closed", "merged"},
    "parked": {"closed", "worked"},
    "closed": set(),
    "merged": set(),
}


def check_transition(old: str, new: str, *, section: str = "A1 §2.1") -> None:
    if old == new:
        return
    if new not in LEGAL.get(old, set()):
        raise HtError(
            f"illegal status transition {old} -> {new} ({section})"
        )
