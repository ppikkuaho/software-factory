"""Increment 1 — load-bearing STRENGTHENING (mutation-review gate).

Two gaps the Increment-1 review flagged where "tests pass == correct" did not robustly hold:

  1. clock.now_utc UTC-ness was only discriminating on a NON-UTC machine (the original
     offset/instant checks pass for a local-time impl when CI happens to run at UTC). Force a
     non-UTC local TZ so the F-019 local-clock bug is caught on ANY machine.
  2. store.append_framed's one-physical-line precondition (no raw newline) was added as a guard;
     lock it so a regression that drops the guard is caught.

Authoritative: DAEMON §4.6 (the canonical UTC clock; F-019 was a UTC-vs-local stale misdiagnosis),
§4.4 (the framed single-line WAL record the byte-len prefix validates).
"""

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import harnessd.clock as clock
import harnessd.store as store


# --------------------------------------------------------------------------------------
# now_utc — machine-INDEPENDENT F-019 kill: force a non-UTC local TZ.
# (Mutants caught on ANY machine: datetime.now().astimezone() -> +5:30 offset;
#  datetime.now().replace(tzinfo=utc) -> instant 5.5h off true UTC.)
# --------------------------------------------------------------------------------------

def test_now_utc_is_utc_under_forced_nonutc_tz():
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Kolkata"  # UTC+5:30, no DST — local is unambiguously != UTC
        time.tzset()

        before = datetime.now(timezone.utc)
        s = clock.now_utc()
        after = datetime.now(timezone.utc)
        dt = datetime.fromisoformat(s)

        # Offset must be UTC even though the local zone is +05:30 (kills local-astimezone F-019 bug).
        assert dt.utcoffset() == timedelta(0), (
            f"now_utc must report +00:00 even under TZ=Asia/Kolkata, got {dt.utcoffset()!r} (F-019 local bug)"
        )
        # Instant must be the TRUE UTC instant (kills local-wall-clock-mislabeled-as-UTC: that lands +5:30 ahead).
        assert before <= dt <= after, (
            f"now_utc instant {dt.isoformat()} not within [{before.isoformat()}, {after.isoformat()}] "
            "— local wall-clock mislabeled as UTC?"
        )
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


# --------------------------------------------------------------------------------------
# append_framed — the one-physical-line precondition is enforced (no raw newline).
# (Mutant caught: dropping the guard would let a newline payload split a record and trigger
#  a FALSE torn-tail corruption-halt in load_wal at Increment 2.)
# --------------------------------------------------------------------------------------

def test_append_framed_rejects_raw_newline_payload(tmp_path):
    p = tmp_path / "wal.log"
    with pytest.raises(ValueError):
        store.append_framed(p, "line1\nline2")
    # nothing partial was written by the rejected call
    assert not p.exists() or p.read_text() == ""


def test_append_framed_accepts_clean_payload(tmp_path):
    p = tmp_path / "wal.log"
    store.append_framed(p, '{"ok": true}')
    text = p.read_text()
    assert text.endswith("\n")
    prefix, payload = text[:-1].split("\t", 1)
    assert int(prefix) == len(payload.encode("utf-8"))
    assert payload == '{"ok": true}'
