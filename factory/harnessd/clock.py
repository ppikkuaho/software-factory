"""The ONE canonical clock — all freshness/lease/age math goes through here.

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.2 (frozen clock.py interface) + §1 module table (clock.py row).
  - DAEMON §4.6, WATCHDOG §3.3 (every freshness/lease computation funnels through this clock).
  - PORT-and-FIX of now_iso L167 / parse_iso_timestamp L265 (control_plane.py prior art).

LOAD-BEARING property (the F-019 mutant kill):
    now_utc() returns a tz-AWARE UTC ISO-8601 instant — the TRUE UTC instant, not the local
    wall clock mislabeled with a non-UTC offset. The recovered now_iso() used
    ``datetime.now().astimezone().isoformat()``, which stamps the LOCAL offset and was the
    root of the F-019 UTC-vs-local stale misdiagnosis. We use ``datetime.now(timezone.utc)``
    so both the offset (+00:00) AND the absolute instant are correct on any machine.

Because every age/lease comparison routes through ``age_seconds`` (which works on absolute
instants via tz-aware ``parse_iso``), offset-vs-instant bugs cannot creep into freshness math.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> str:
    """Return the current instant as a tz-aware UTC ISO-8601 string (offset +00:00).

    Uses ``datetime.now(timezone.utc)`` — the TRUE UTC instant with a UTC tzinfo — NOT
    ``datetime.now().astimezone()`` (which carries the machine's LOCAL offset; the F-019 bug).
    """
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string into a datetime, preserving its offset (PORT L265).

    A UTC input yields a +00:00 datetime; a non-UTC offset is preserved as the same absolute
    instant. Callers normalize to UTC for instant comparisons via ``.astimezone(timezone.utc)``.
    """
    return datetime.fromisoformat(s)


def age_seconds(then_iso: str, *, now: str | None = None) -> float:
    """Elapsed seconds between ``then_iso`` and ``now`` (defaults to ``now_utc()``).

    Both endpoints are parsed to tz-aware datetimes and the difference is taken on ABSOLUTE
    instants, so the result is offset-invariant: two equal instants written in different
    offsets yield 0.0, never an offset-arithmetic error. A just-now ``then`` yields a
    near-zero, non-negative age.
    """
    then_dt = parse_iso(then_iso)
    now_dt = parse_iso(now) if now is not None else parse_iso(now_utc())
    return (now_dt - then_dt).total_seconds()
