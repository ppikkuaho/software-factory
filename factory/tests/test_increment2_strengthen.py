"""Increment 2 — load-bearing STRENGTHENING (mutation-review gate).

Gaps the Increment-2 review flagged where "tests pass == correct" was not robustly held:

  1. next_seq: the headline (d) fixture had contiguous surviving seqs [1,2], so max(seq)+1 and
     count(len)+1 BOTH yield 3 — a count-based next_seq mutant survived. FORK-SEQ requires
     last (MAX) WAL seq + 1; force a GAP so the two formulas diverge.
  2. fail-closed exception specificity: the originals accepted raises((ValueError, Exception)),
     which a stray AttributeError/TypeError from a different bug could satisfy. load_wal raises a
     SPECIFIC WALCorruptionError on mid-file corruption — assert exactly that.
  3. position-decides boundary: the SAME corruption truncates as a FINAL line but fails-closed as a
     NON-final line — pin it so it's position (final vs non-final), not corruption, that decides.

Authoritative: IMPLEMENTATION-PLAN §2.4 + §4.4; FORK-SEQ (derive-from-WAL) + FORK-CRC (crc32 backstop).
"""

import json

import pytest

import harnessd.ledger as ledger


def _frame(payload: str) -> str:
    return f"{len(payload.encode('utf-8'))}\t{payload}\n"


def _rec(seq: int) -> dict:
    return ledger.build_wal_record(
        node_address="proj/x#exec", event="transition",
        from_state="planned", to_state="claimed",
        expected_generation=0, generation=1, lease_epoch=1, owner_token="tok",
        binding_delta={"state": "claimed"}, summary="s", artifacts=[], seq=seq,
    )


# --------------------------------------------------------------------------------------
# FORK-SEQ: next_seq == MAX(seq) + 1, NOT count + 1. A gap [1, 5] discriminates (6 vs 3).
# --------------------------------------------------------------------------------------

def test_next_seq_is_max_seq_plus_one_not_count(tmp_path):
    wal = tmp_path / "run-ledger.jsonl"
    ledger.append_wal(_rec(1), wal_path=wal)
    ledger.append_wal(_rec(5), wal_path=wal)  # gap -> seqs [1,5]; max+1=6, count+1=3
    assert ledger.next_seq(wal_path=wal) == 6, "next_seq must be last (max) WAL seq + 1 (FORK-SEQ), not count + 1"


# --------------------------------------------------------------------------------------
# Fail-closed raises the SPECIFIC WALCorruptionError on mid-file corruption (not a generic error).
# --------------------------------------------------------------------------------------

def test_load_wal_raises_walcorruption_specifically_on_non_final(tmp_path):
    wal = tmp_path / "run-ledger.jsonl"
    torn = json.dumps(_rec(1))
    good = json.dumps(_rec(2))
    bad_prefix = len(torn.encode("utf-8")) + 7  # deliberately wrong byte-len -> torn
    # torn line FIRST (non-final), clean line LAST
    wal.write_text(f"{bad_prefix}\t{torn}\n" + _frame(good), encoding="utf-8")
    with pytest.raises(ledger.WALCorruptionError):
        ledger.load_wal(wal_path=wal)


# --------------------------------------------------------------------------------------
# Position decides: the SAME corruption truncates as a FINAL line, fails-closed as NON-final.
# --------------------------------------------------------------------------------------

def test_same_corruption_truncates_when_final(tmp_path):
    wal = tmp_path / "run-ledger.jsonl"
    good = json.dumps(_rec(1))
    torn = json.dumps(_rec(2))
    bad_prefix = len(torn.encode("utf-8")) + 7
    # clean line FIRST, SAME torn frame LAST -> truncate-and-continue (no raise)
    wal.write_text(_frame(good) + f"{bad_prefix}\t{torn}\n", encoding="utf-8")
    out = ledger.load_wal(wal_path=wal)
    assert [r["seq"] for r in out] == [1], "a torn FINAL line truncates to the clean prefix"
