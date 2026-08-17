"""Increment 1 Done-test (clause 2 — the canonical clock): clock.now_utc() returns
a tz-AWARE UTC ISO-8601 instant, NOT a local-shifted one.

Authoritative source: IMPLEMENTATION-PLAN.md §2.2 (frozen clock.py interface) +
DAEMON §4.6 / WATCHDOG §3.3 (all freshness/lease math goes through the one clock),
transcribed below as the single source of truth for this suite.

Frozen interface under test (§2.2):
    def now_utc() -> str            # tz-aware UTC ISO-8601 (MUST be UTC, not local)
    def parse_iso(s: str) -> datetime
    def age_seconds(then_iso: str, *, now: str | None = None) -> float

LOAD-BEARING property (where "tests pass == correct" lives):
    now_utc()'s tzinfo is UTC AND the instant equals the TRUE UTC instant — NOT the
    local wall clock dressed up with a non-UTC offset. The recovered now_iso() (L167)
    used ``datetime.now().astimezone().isoformat()`` which carries the LOCAL offset
    (F-019: a UTC-vs-local stale misdiagnosis). These tests are written to FAIL that
    exact mutant.

These tests fail RED until harnessd/clock.py exists and is correct.
"""

from datetime import datetime, timedelta, timezone

import pytest

# Direct import (NOT importorskip): until the builder creates harnessd/clock.py this is a
# collection-time ImportError, which pytest reports as a RED error for every test in the
# file. A skip would wrongly read as "satisfied".
import harnessd.clock as clock


# ======================================================================================
# now_utc() — tz-aware UTC, true instant (NOT local-shifted). The F-019 mutant kill.
# ======================================================================================


def test_now_utc_returns_str():
    assert isinstance(clock.now_utc(), str)


def test_now_utc_is_tz_aware():
    """The returned string MUST parse to a tz-AWARE datetime (offset present)."""
    dt = datetime.fromisoformat(clock.now_utc())
    assert dt.tzinfo is not None, "now_utc() must be tz-aware, got a naive timestamp"
    assert dt.utcoffset() is not None


def test_now_utc_offset_is_zero():
    """LOAD-BEARING: the offset is UTC (zero), not the machine's local offset.

    FLAG-FOR-MUTANT: ``datetime.now().astimezone().isoformat()`` (the F-019 local bug)
    carries the local offset. On any machine NOT at UTC this assertion fails. To make
    the test machine-independent (so it kills the mutant even in a UTC-configured CI),
    we ALSO check the instant directly below — but a zero offset is the first signal.
    """
    dt = datetime.fromisoformat(clock.now_utc())
    assert dt.utcoffset() == timedelta(0), (
        f"now_utc() must report a +00:00 (UTC) offset, got {dt.utcoffset()!r}. "
        "A local astimezone() offset (F-019) fails here."
    )


def test_now_utc_instant_equals_true_utc():
    """LOAD-BEARING (machine-independent mutant kill): the *instant* equals true UTC.

    This is the assertion that catches the F-019 bug even on a UTC-configured machine
    where the offset alone would look fine for the wrong reason. We compare the parsed
    instant against ``datetime.now(timezone.utc)`` taken around the call. A local-clock
    implementation that mislabels local wall-time as UTC (e.g. constructs a naive local
    ``datetime.now()`` and stamps ``+00:00`` / ``Z`` onto it) lands far from true UTC on
    any non-UTC machine and is rejected.
    """
    before = datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(clock.now_utc())
    after = datetime.now(timezone.utc)

    # Compare absolute instants. ``parsed`` is aware; normalize to UTC for the bracket.
    parsed_utc = parsed.astimezone(timezone.utc)
    assert before - timedelta(seconds=5) <= parsed_utc <= after + timedelta(seconds=5), (
        f"now_utc() instant {parsed_utc.isoformat()} is not the true UTC instant "
        f"(expected within [{before.isoformat()}, {after.isoformat()}]). A local-clock "
        "value mislabeled as UTC (F-019) fails here."
    )


def test_now_utc_roundtrips_through_parse_iso():
    """now_utc() output must be parseable by the module's own parse_iso()."""
    dt = clock.parse_iso(clock.now_utc())
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


# ======================================================================================
# parse_iso() — PORT of parse_iso_timestamp L265 (datetime.fromisoformat).
# ======================================================================================


def test_parse_iso_returns_datetime():
    dt = clock.parse_iso("2026-06-05T12:00:00+00:00")
    assert isinstance(dt, datetime)


def test_parse_iso_preserves_utc_offset():
    dt = clock.parse_iso("2026-06-05T12:00:00+00:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_iso_preserves_non_utc_offset():
    """A non-UTC offset in the input is preserved as the same absolute instant."""
    dt = clock.parse_iso("2026-06-05T12:00:00+02:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=2)
    # Same instant as 10:00 UTC.
    assert dt.astimezone(timezone.utc) == datetime(
        2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc
    )


# ======================================================================================
# age_seconds() — elapsed seconds between a past ISO instant and now (or an explicit now).
# ======================================================================================


def test_age_seconds_explicit_now_is_positive_elapsed():
    """age_seconds(then, now=later) == the elapsed seconds (then earlier than now)."""
    then = "2026-06-05T12:00:00+00:00"
    now = "2026-06-05T12:00:30+00:00"
    assert clock.age_seconds(then, now=now) == pytest.approx(30.0)


def test_age_seconds_is_a_float():
    then = "2026-06-05T12:00:00+00:00"
    now = "2026-06-05T12:00:01+00:00"
    age = clock.age_seconds(then, now=now)
    assert isinstance(age, float)


def test_age_seconds_zero_when_same_instant():
    ts = "2026-06-05T12:00:00+00:00"
    assert clock.age_seconds(ts, now=ts) == pytest.approx(0.0)


def test_age_seconds_is_offset_invariant():
    """LOAD-BEARING: age is computed on absolute instants, so equal instants in
    DIFFERENT offsets yield zero age — not an offset-arithmetic error.

    14:00+02:00 and 12:00+00:00 are the SAME instant. A buggy clock that subtracts
    naive wall-clock components (ignoring tzinfo) would report a 2-hour age here.
    """
    then = "2026-06-05T14:00:00+02:00"  # == 12:00:00 UTC
    now = "2026-06-05T12:00:00+00:00"
    assert clock.age_seconds(then, now=now) == pytest.approx(0.0), (
        "age_seconds must compare absolute instants, not naive wall-clock fields"
    )


def test_age_seconds_default_now_uses_now_utc():
    """With no explicit now=, age is measured against the canonical now_utc() — i.e.
    a just-now timestamp has a near-zero, non-negative age.
    """
    age = clock.age_seconds(clock.now_utc())
    assert age >= 0.0
    assert age < 5.0
