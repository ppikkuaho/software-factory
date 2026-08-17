"""Increment 2 FROZEN acceptance — ledger + WAL torn-tail (THE HEADLINE FIX).

Authoritative source: IMPLEMENTATION-PLAN.md §2.4 (frozen ledger.py interface) +
§4.4-box (intent-first ordering + the torn-tail CORRECTION) + DAEMON §4.3 (binding
atomic-replace, single-writer rule) / §4.4 (intent-first crash-atomicity + torn-tail
tolerance). Transcribed below as the single source of truth for this suite; the
recovered prior art (control_plane.py load_ledger L209-225) is the ANTI-pattern this
rewrite corrects, NOT the contract.

THE FIX this suite proves: recovered ``load_ledger`` RAISES ``ValueError`` on ANY
``json.JSONDecodeError`` (L220) — a single torn final line bricked the entire boot-recovery
path. v1 ``load_wal`` survives a crash mid-append: TRUNCATE the torn FINAL line and continue;
FAIL CLOSED on any NON-FINAL corruption (a mid-file corruption is not a crash artifact).

Frozen interface under test (§2.4):
    def read_binding(node_address: str) -> dict | None
    def all_nodes() -> dict[str, dict]
    def write_binding(candidate_map: dict, *, _lock_held: bool) -> None
    def append_wal(record: dict) -> None
    def load_wal() -> list[dict]
    def next_seq() -> int
    def build_wal_record(*, node_address, event, from_state, to_state, expected_generation,
                         generation, lease_epoch, owner_token, binding_delta, summary,
                         artifacts, seq) -> dict

RESOLVED FORKS this suite binds to (user-delegated 2026-06-05):
  * FORK-CRC: build_wal_record always emits crc32(payload); load_wal treats
    crc32(payload) != record['crc32'] as a torn signal ALONGSIDE the length-prefix mismatch.
    This catches a silently-SPLIT record whose declared prefix STILL equals the surviving
    byte count (the case the length-prefix alone misses). NO 'len' field inside the json.
  * FORK-SEQ: next_seq() = last WAL seq + 1, computed on load from the WAL itself. No
    persisted counter. 0/1 base on an empty WAL.

PATH INJECTION CONTRACT (REQUIRED, §2.4 "make the WAL + binding paths injectable"): the WAL
and binding paths MUST be injectable so tests use tmp_path. This suite calls every public
function through a small adapter (``_LEDGER_CALL``) that tries, in order: a per-call keyword
(``wal_path=`` / ``binding_path=`` / ``path=``), then a module ``RUNTIME_ROOT`` rebind. The
builder may pick EITHER injection style; the adapter discovers which the module exposes so the
contract — not a particular spelling — is what's frozen. If NEITHER is exposed the test fails
loudly (an un-injectable ledger is non-conformant with §2.4).

BINDING-LEDGER SERIALIZATION (fork settled here, reported to the orchestrator): §2.4 names the
file ``binding-ledger.yaml`` and DAEMON §3.4 shows a YAML example, but the design treats the
file as DAEMON-WRITTEN ONLY (§4.3: "CLIs are clients, never writers"; "No external process ever
read-modify-replaces binding-ledger.yaml directly"). The serialization FORMAT is nowhere pinned
as load-bearing — the ``.yaml`` is the recovered ``manifest.yaml`` naming carried forward, not a
content requirement. So per the task directive these tests assert the binding ledger
SEMANTICALLY (write_binding -> read_binding/all_nodes round-trip + the _lock_held guard + the
atomic-replace write path) and DO NOT hard-pin JSON-vs-YAML on disk. Recommendation reported:
JSON (stdlib, no new dep, round-trips via store.normalize_scalars, consistent with WAL framing).

These tests fail RED until harnessd/ledger.py exists and is correct.
"""

from __future__ import annotations

import copy
import json
import zlib
from pathlib import Path

import pytest

# Direct import (NOT importorskip): until the builder creates harnessd/ledger.py this is a
# collection-time ImportError, which pytest reports as a RED error for every test in the file.
# A skip would wrongly read as "satisfied" — RED is the required starting state.
import harnessd.ledger as ledger
import harnessd.store as store


# ======================================================================================
# Path-injection adapter — see PATH INJECTION CONTRACT in the module docstring.
# ======================================================================================

def _call(fn, *, wal_path=None, binding_path=None, **kwargs):
    """Invoke a ledger function, injecting the tmp_path WAL/binding location however the
    module accepts it. Tries per-call kwargs first, then a RUNTIME_ROOT module rebind."""
    import inspect

    sig_params = set()
    try:
        sig_params = set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        pass

    inject = {}
    if wal_path is not None:
        if "wal_path" in sig_params:
            inject["wal_path"] = wal_path
        elif "path" in sig_params:
            inject["path"] = wal_path
    if binding_path is not None:
        if "binding_path" in sig_params:
            inject["binding_path"] = binding_path
        elif "path" in sig_params and "wal_path" not in inject:
            inject["path"] = binding_path

    if inject:
        return fn(*(), **{**kwargs, **inject})

    # Fall back to a module RUNTIME_ROOT rebind (the other supported injection style).
    if hasattr(ledger, "RUNTIME_ROOT") and (wal_path is not None or binding_path is not None):
        root = (wal_path or binding_path).parent
        old = ledger.RUNTIME_ROOT
        ledger.RUNTIME_ROOT = root
        try:
            return fn(**kwargs)
        finally:
            ledger.RUNTIME_ROOT = old

    pytest.fail(
        "ledger is not injectable per §2.4: expose a per-call wal_path=/binding_path=/path= "
        "keyword OR a module RUNTIME_ROOT default so tests can target tmp_path"
    )


def _wal_file(tmp_path: Path) -> Path:
    # With a RUNTIME_ROOT-style module, load_wal must find this exact name under the root.
    return tmp_path / "run-ledger.jsonl"


# ======================================================================================
# Frame helpers — we craft on-disk bytes DIRECTLY for the torn/split cases so the tests
# discriminate a wrong load_wal regardless of how the module spells its framer.
# ======================================================================================

def _crc32(payload_obj: dict) -> int:
    """crc32 over the json CONTENT of the record's NON-crc32 fields, deterministic key order
    (§2.4: crc32 measures the json content, never the length, never itself — not circular)."""
    body = {k: v for k, v in payload_obj.items() if k != "crc32"}
    content = json.dumps(body, sort_keys=True, ensure_ascii=True)
    return zlib.crc32(content.encode("utf-8")) & 0xFFFFFFFF


def _good_record(seq: int, node="payments/gateway/stripe#exec") -> dict:
    """A self-consistent WAL record (crc32 matches its content) crafted without the module —
    so the on-disk-bytes tests don't depend on build_wal_record being correct."""
    rec = {
        "ts": "2026-06-05T10:14:00+00:00",
        "seq": seq,
        "node_address": node,
        "event": "spawned",
        "actor": "harnessd",
        "from_state": "spawning",
        "to_state": "running",
        "expected_generation": seq - 1,
        "generation": seq,
        "lease_epoch": 1,
        "owner_token": f"{node}:sub:uuid:1",
        "binding_delta": {"liveness_state": "working"},
        "summary": f"row {seq}",
        "artifacts": ["report.md"],
    }
    rec["crc32"] = _crc32(rec)
    return rec


def _frame(payload: str) -> str:
    """The on-disk frame store.append_framed produces: '<byte-len>\\t<payload>\\n'."""
    return f"{len(payload.encode('utf-8'))}\t{payload}\n"


def _good_frame(seq: int, node="payments/gateway/stripe#exec") -> str:
    return _frame(json.dumps(_good_record(seq, node)))


# ======================================================================================
# (c) ROUND-TRIP — append_wal then load_wal returns the record; on-disk frame is
#     '<byte-len>\t<json>'.  (Done-test c)
# ======================================================================================

def test_append_wal_then_load_wal_roundtrips(tmp_path):
    wal = _wal_file(tmp_path)
    rec = ledger.build_wal_record(
        node_address="payments/gateway/stripe#exec", event="spawned",
        from_state="spawning", to_state="running", expected_generation=6,
        generation=7, lease_epoch=3, owner_token="payments#exec:sub:uuid:3",
        binding_delta={"session_uuid": "abc", "liveness_state": "working"},
        summary="opened actor", artifacts=["report.md"], seq=412,
    )
    _call(ledger.append_wal, wal_path=wal, record=rec)
    loaded = _call(ledger.load_wal, wal_path=wal)
    assert loaded == [rec], "append_wal -> load_wal must round-trip the record byte-faithfully"


def test_append_wal_on_disk_frame_is_bytelen_tab_json(tmp_path):
    wal = _wal_file(tmp_path)
    rec = ledger.build_wal_record(
        node_address="a/b#exec", event="spawned", from_state="planned",
        to_state="running", expected_generation=0, generation=1, lease_epoch=1,
        owner_token="a/b#exec:sub:uuid:1", binding_delta={"x": 1}, summary="s",
        artifacts=[], seq=1,
    )
    _call(ledger.append_wal, wal_path=wal, record=rec)
    text = wal.read_text(encoding="utf-8")
    assert text.endswith("\n"), "each WAL line is newline-terminated"
    line = text[:-1]
    assert "\t" in line, "the frame must be '<byte-len>\\t<json>'"
    prefix, payload = line.split("\t", 1)
    # The <byte-len> PREFIX is the SOLE length authority (no in-payload 'len').
    assert int(prefix) == len(payload.encode("utf-8")), (
        "the byte-len prefix must equal the UTF-8 byte length of the json payload"
    )
    parsed = json.loads(payload)
    assert parsed == rec
    assert "len" not in parsed, "NO 'len' field inside the json (the prefix is the only length)"


def test_append_wal_hands_raw_json_to_framer_not_preframed(tmp_path):
    """append_wal NEVER pre-frames: the payload between the prefix and newline is exactly
    json.dumps(record) — not a doubly-framed string."""
    wal = _wal_file(tmp_path)
    rec = _good_record(1)
    _call(ledger.append_wal, wal_path=wal, record=rec)
    payload = wal.read_text(encoding="utf-8")[:-1].split("\t", 1)[1]
    # A single tab in the line; the payload itself is bare json (no second '<len>\t').
    assert payload.lstrip().startswith("{"), "payload must be raw json, not a re-framed line"
    assert json.loads(payload) == rec


# ======================================================================================
# (a) LAST-LINE TRUNCATION — a torn FINAL line is truncated; load_wal returns the clean
#     prefix and CONTINUES (does NOT raise).  (Done-test a — the headline truncate-tail)
# ======================================================================================

def test_load_wal_truncates_torn_final_line_and_returns_clean_prefix(tmp_path):
    wal = _wal_file(tmp_path)
    good = _good_frame(1) + _good_frame(2)
    # A torn FINAL line: a length prefix that does NOT match the (truncated) payload bytes —
    # exactly what a crash mid-append leaves behind.
    torn_tail = "9999\t{\"seq\": 3, \"node_a"  # no newline, prefix lies, json incomplete
    wal.write_text(good + torn_tail, encoding="utf-8")

    loaded = _call(ledger.load_wal, wal_path=wal)
    seqs = [r["seq"] for r in loaded]
    assert seqs == [1, 2], (
        "load_wal must TRUNCATE the torn FINAL line and return the clean prefix [1,2] — "
        "a torn append whose binding atomic-replace never landed (§4.4 truncate-and-continue). "
        "An impl that RAISES on a torn tail bricks boot-recovery (the recovered L220 bug)."
    )


def test_load_wal_truncates_torn_final_line_even_with_trailing_newline(tmp_path):
    """The torn final line is the LAST record even when it ended up newline-terminated but
    its prefix/json is inconsistent — still truncate-and-continue, never raise."""
    wal = _wal_file(tmp_path)
    good = _good_frame(1)
    torn_final = "5\t{\"seq\": 2}\n"  # prefix 5 != byte length of the json -> torn, but final
    wal.write_text(good + torn_final, encoding="utf-8")
    loaded = _call(ledger.load_wal, wal_path=wal)
    assert [r["seq"] for r in loaded] == [1], "torn final line truncated even if newline-terminated"


# ======================================================================================
# (b) NON-FINAL CORRUPTION — a torn NON-FINAL line FAILS CLOSED (raises).  (Done-test b)
#     This is the truncate-tail / fail-closed BOUNDARY: an impl that silently swallows
#     non-final corruption MUST fail this test.
# ======================================================================================

def test_load_wal_fails_closed_on_torn_non_final_line(tmp_path):
    wal = _wal_file(tmp_path)
    # good line 1, then a CORRUPT middle line (broken json), then a perfectly good final line.
    corrupt_middle = _frame('{"seq": 2, "node_address": "BROKEN')  # unterminated json string
    content = _good_frame(1) + corrupt_middle + _good_frame(3)
    wal.write_text(content, encoding="utf-8")

    with pytest.raises((ValueError, Exception)) as exc:
        _call(ledger.load_wal, wal_path=wal)
    # It must be a deliberate corruption-halt, not an AttributeError/TypeError from sloppy code.
    assert not isinstance(exc.value, (AttributeError, TypeError)), (
        f"non-final corruption must FAIL CLOSED with a deliberate raise, got {exc.value!r}"
    )


def test_load_wal_non_final_prefix_mismatch_fails_closed(tmp_path):
    """A NON-final line whose byte-len prefix disagrees with the payload (and is NOT the tail)
    is mid-file corruption -> fail closed. Discriminates a 'truncate any torn line' mutant."""
    wal = _wal_file(tmp_path)
    bad_middle = "3\t" + json.dumps(_good_record(2)) + "\n"  # prefix 3 != real byte length
    content = _good_frame(1) + bad_middle + _good_frame(3)
    wal.write_text(content, encoding="utf-8")
    with pytest.raises(Exception):
        _call(ledger.load_wal, wal_path=wal)


# ======================================================================================
# (d) PRODUCER-SIDE INTERRUPTED APPEND — write a full frame via append_wal, then a PARTIAL
#     frame (truncate mid-payload, simulating fsync-not-reached). load_wal returns the clean
#     prefix and next_seq() is correct (last good seq + 1).  (Done-test d — WRITE path safe)
# ======================================================================================

def test_load_wal_recovers_producer_side_interrupted_append(tmp_path):
    wal = _wal_file(tmp_path)
    # Two real appends through the producer path (proves the framer write path, not only
    # hand-authored fixtures).
    r1 = ledger.build_wal_record(
        node_address="a/b#exec", event="spawned", from_state="planned", to_state="running",
        expected_generation=0, generation=1, lease_epoch=1, owner_token="a/b#exec:s:u:1",
        binding_delta={"x": 1}, summary="one", artifacts=[], seq=1,
    )
    r2 = ledger.build_wal_record(
        node_address="a/b#exec", event="heartbeat", from_state="running", to_state="running",
        expected_generation=1, generation=2, lease_epoch=1, owner_token="a/b#exec:s:u:1",
        binding_delta={"x": 2}, summary="two", artifacts=[], seq=2,
    )
    _call(ledger.append_wal, wal_path=wal, record=r1)
    _call(ledger.append_wal, wal_path=wal, record=r2)

    # Now simulate a crash mid-append of a THIRD frame: append a partial frame (a real
    # byte-len prefix for a record whose payload is truncated before the writer fsync'd /
    # completed the line). fsync-not-reached == a torn FINAL line on disk.
    full_payload = json.dumps(_good_record(3))
    partial = _frame(full_payload)[: len(_frame(full_payload)) // 2]  # cut mid-payload, no \n
    with wal.open("a", encoding="utf-8") as h:
        h.write(partial)

    loaded = _call(ledger.load_wal, wal_path=wal)
    assert [r["seq"] for r in loaded] == [1, 2], (
        "the torn producer-side tail is truncated; the clean prefix [1,2] loads "
        "(proves the WRITE path is torn-tail-safe, not only hand-authored fixtures)"
    )
    assert _call(ledger.next_seq, wal_path=wal) == 3, (
        "next_seq must be last good seq + 1 == 3 (FORK-SEQ: from the WAL, not a persisted "
        "counter); a torn tail must NOT poison the allocation"
    )


def test_next_seq_on_empty_wal_is_base(tmp_path):
    wal = _wal_file(tmp_path)  # never created
    base = _call(ledger.next_seq, wal_path=wal)
    assert base in (0, 1), "next_seq on an empty/absent WAL is the 0/1 base (FORK-SEQ)"


def test_next_seq_is_max_seq_plus_one(tmp_path):
    wal = _wal_file(tmp_path)
    wal.write_text(_good_frame(7) + _good_frame(8) + _good_frame(9), encoding="utf-8")
    assert _call(ledger.next_seq, wal_path=wal) == 10, "next_seq = last (max) WAL seq + 1"


def test_load_wal_empty_when_absent(tmp_path):
    wal = _wal_file(tmp_path)  # not created
    assert _call(ledger.load_wal, wal_path=wal) == [], "absent WAL loads as empty, never raises"


# ======================================================================================
# (e) SILENTLY-SPLIT NON-FINAL RECORD whose declared prefix STILL parses as the surviving
#     byte count -> caught by crc32 (the FORK-CRC backstop) -> FAIL CLOSED.  (Done-test e)
#     This is the case the length-prefix ALONE misses: an impl WITHOUT the crc32 backstop
#     accepts the split record silently and MUST fail this test.
# ======================================================================================

def test_load_wal_crc32_catches_split_non_final_record_fails_closed(tmp_path):
    wal = _wal_file(tmp_path)
    rec = _good_record(2)
    payload = json.dumps(rec)
    # Corrupt the payload IN A WAY THAT PRESERVES ITS BYTE LENGTH (so the length-prefix still
    # matches) but breaks content integrity -> only crc32 can catch it. Flip a value char.
    assert '"summary": "row 2"' in payload
    corrupted = payload.replace('"summary": "row 2"', '"summary": "row X"')
    assert len(corrupted.encode("utf-8")) == len(payload.encode("utf-8")), (
        "fixture invariant: the corruption must preserve byte length so the prefix STILL matches"
    )
    # Build the frame with the ORIGINAL (correct) byte-len prefix — it still matches the
    # corrupted payload's length, so the length check passes. Only crc32(payload) != stored crc
    # reveals the tamper. This line is NON-FINAL.
    split_line = f"{len(corrupted.encode('utf-8'))}\t{corrupted}\n"
    content = _good_frame(1) + split_line + _good_frame(3)
    wal.write_text(content, encoding="utf-8")

    with pytest.raises(Exception) as exc:
        _call(ledger.load_wal, wal_path=wal)
    assert not isinstance(exc.value, (AttributeError, TypeError)), (
        f"crc32 split-record backstop must FAIL CLOSED deliberately, got {exc.value!r}. "
        "An impl WITHOUT the FORK-CRC backstop accepts this silently and fails this test."
    )


def test_load_wal_crc32_split_on_final_line_truncates(tmp_path):
    """Symmetric to (e): a crc-broken-but-length-matching FINAL line is still a torn tail ->
    truncate-and-continue (the crc backstop is a torn SIGNAL, not unconditionally fail-closed)."""
    wal = _wal_file(tmp_path)
    rec = _good_record(2)
    payload = json.dumps(rec)
    corrupted = payload.replace('"summary": "row 2"', '"summary": "row X"')
    final_line = f"{len(corrupted.encode('utf-8'))}\t{corrupted}\n"
    wal.write_text(_good_frame(1) + final_line, encoding="utf-8")
    loaded = _call(ledger.load_wal, wal_path=wal)
    assert [r["seq"] for r in loaded] == [1], (
        "a crc-broken FINAL line is a torn tail -> truncated; the clean prefix [1] loads"
    )


# ======================================================================================
# build_wal_record SHAPE — §2.4 frozen field set, crc32 over content (not len, not itself),
# generation == expected+1 for transitions, NO 'len' field, journal-only rows may omit effects.
# ======================================================================================

def test_build_wal_record_has_frozen_shape(tmp_path):
    rec = ledger.build_wal_record(
        node_address="payments/gateway/stripe#exec", event="spawned",
        from_state="spawning", to_state="running", expected_generation=6, generation=7,
        lease_epoch=3, owner_token="payments#exec:sub:uuid:3",
        binding_delta={"session_uuid": "abc"}, summary="opened actor",
        artifacts=["report.md"], seq=412,
    )
    for field in ("ts", "seq", "node_address", "event", "actor", "crc32", "from_state",
                  "to_state", "expected_generation", "generation", "lease_epoch",
                  "owner_token", "binding_delta", "summary", "artifacts"):
        assert field in rec, f"build_wal_record must include the frozen field {field!r}"
    assert rec["actor"] == "harnessd", "actor is fixed to 'harnessd' (the single writer)"
    assert rec["seq"] == 412
    assert rec["node_address"] == "payments/gateway/stripe#exec"
    assert "len" not in rec, "NO 'len' field in the record (the framed prefix is the only length)"


def test_build_wal_record_generation_is_expected_plus_one(tmp_path):
    rec = ledger.build_wal_record(
        node_address="a/b#exec", event="spawned", from_state="planned", to_state="running",
        expected_generation=6, generation=7, lease_epoch=1, owner_token="a/b#exec:s:u:1",
        binding_delta={}, summary="", artifacts=[], seq=1,
    )
    assert rec["generation"] == rec["expected_generation"] + 1, (
        "post-commit generation must equal expected_generation + 1 (the CAS post-image)"
    )


def test_build_wal_record_crc32_matches_content_and_is_not_circular(tmp_path):
    """crc32 is computed over the json CONTENT of the OTHER fields (deterministic key order),
    NOT over a length and NOT over a field measuring the json that contains it."""
    rec = ledger.build_wal_record(
        node_address="a/b#exec", event="spawned", from_state="planned", to_state="running",
        expected_generation=0, generation=1, lease_epoch=1, owner_token="a/b#exec:s:u:1",
        binding_delta={"k": "v"}, summary="s", artifacts=["r.md"], seq=1,
    )
    assert _crc32(rec) == rec["crc32"], (
        "crc32 must match a recompute over the record's non-crc32 fields (content integrity)"
    )
    # And it must round-trip through the load path's torn check as NON-torn (sanity: the
    # record append_wal writes is accepted by load_wal).
    assert isinstance(rec["crc32"], int)


def test_build_wal_record_roundtrips_clean_through_load(tmp_path):
    """A record built by build_wal_record and appended by append_wal loads back NON-torn —
    the producer crc32 and the loader crc32 check agree (no spurious corruption-halt)."""
    wal = _wal_file(tmp_path)
    rec = ledger.build_wal_record(
        node_address="a/b#exec", event="spawned", from_state="planned", to_state="running",
        expected_generation=0, generation=1, lease_epoch=1, owner_token="a/b#exec:s:u:1",
        binding_delta={"k": "v"}, summary="s", artifacts=["r.md"], seq=1,
    )
    _call(ledger.append_wal, wal_path=wal, record=rec)
    assert _call(ledger.load_wal, wal_path=wal) == [rec]


def test_journal_only_row_omits_binding_effect(tmp_path):
    """The record builder can express an attribution-only row with no binding effect."""
    rec = ledger.build_wal_record(
        node_address="a/b#exec", event="reconcile_escalation", from_state=None, to_state=None,
        expected_generation=None, generation=None, lease_epoch=1,
        owner_token="a/b#exec:s:u:1", binding_delta=None, summary="observe",
        artifacts=[], seq=5,
    )
    assert rec["event"] == "reconcile_escalation"
    assert not rec["binding_delta"], "a journal-only row has no canonical binding effect"


# ======================================================================================
# (f) write_binding WITHOUT _lock_held=True RAISES — the structural single-writer guard.
#     (Done-test f.) A whole-map replace by anyone but the serialized writer silently
#     clobbers a concurrent write to a DIFFERENT node; per-node CAS can't catch it, so the
#     guard is STRUCTURAL, not convention.
# ======================================================================================

def test_write_binding_without_lock_held_raises(tmp_path):
    binding = tmp_path / "binding-ledger.json"
    with pytest.raises(Exception) as exc:
        _call(ledger.write_binding, binding_path=binding,
              candidate_map={"a/b#exec": {"state": "running"}}, _lock_held=False)
    assert not isinstance(exc.value, (AttributeError, TypeError)), (
        f"write_binding must raise DELIBERATELY on missing _lock_held, got {exc.value!r} "
        "(the structural single-writer guard — DAEMON §4.3 cross-node clobber)"
    )
    # And nothing was written — the guard fires BEFORE any replace.
    assert not binding.exists(), "the lock guard must fire before any write touches disk"


def test_write_binding_lock_held_keyword_is_required_keyword_only(tmp_path):
    """_lock_held is keyword-only (§2.4 signature: write_binding(candidate_map, *, _lock_held)).
    Calling it positionally must NOT smuggle past the guard."""
    binding = tmp_path / "binding-ledger.json"
    with pytest.raises(TypeError):
        # No _lock_held kwarg at all -> TypeError (required keyword-only arg). This pins the
        # signature so a default-True or positional spelling that defeats the guard is rejected.
        _call(ledger.write_binding, binding_path=binding,
              candidate_map={"a/b#exec": {"state": "running"}})


# ======================================================================================
# read_binding / all_nodes / write_binding round-trip (binding ledger semantics —
# serialization-format-agnostic per the fork settled in the module docstring).
# ======================================================================================

def test_write_binding_then_read_binding_roundtrips(tmp_path):
    binding = tmp_path / "binding-ledger.json"
    node = "payments/gateway/stripe#exec"
    candidate = {
        node: {"node_address": node, "state": "running", "generation": 7,
               "owner_token": "payments#exec:sub:uuid:3", "last_applied_seq": 412},
        "other/node#exec": {"node_address": "other/node#exec", "state": "planned",
                            "generation": 0},
    }
    _call(ledger.write_binding, binding_path=binding, candidate_map=candidate, _lock_held=True)

    got = _call(ledger.read_binding, binding_path=binding, node_address=node)
    assert got is not None, "read_binding must return the written node record"
    assert got["state"] == "running" and got["generation"] == 7 and got["last_applied_seq"] == 412


def test_read_binding_absent_node_returns_none(tmp_path):
    binding = tmp_path / "binding-ledger.json"
    _call(ledger.write_binding, binding_path=binding,
          candidate_map={"a/b#exec": {"state": "running"}}, _lock_held=True)
    assert _call(ledger.read_binding, binding_path=binding, node_address="zzz/none#exec") is None


def test_read_binding_absent_file_returns_none(tmp_path):
    binding = tmp_path / "binding-ledger.json"  # never written
    assert _call(ledger.read_binding, binding_path=binding, node_address="a/b#exec") is None


def test_all_nodes_returns_whole_keyed_map(tmp_path):
    binding = tmp_path / "binding-ledger.json"
    candidate = {
        "a/b#exec": {"state": "running", "generation": 1},
        "c/d#exec": {"state": "planned", "generation": 0},
    }
    _call(ledger.write_binding, binding_path=binding, candidate_map=candidate, _lock_held=True)
    got = _call(ledger.all_nodes, binding_path=binding)
    assert set(got.keys()) == {"a/b#exec", "c/d#exec"}, "all_nodes returns the whole keyed map (Option A)"
    assert got["a/b#exec"]["state"] == "running"


def test_all_nodes_absent_file_is_empty_map(tmp_path):
    binding = tmp_path / "binding-ledger.json"  # never written
    assert _call(ledger.all_nodes, binding_path=binding) == {}, "absent binding ledger is an empty map"


def test_write_binding_uses_atomic_replace_no_torn_map(tmp_path, monkeypatch):
    """write_binding goes through store.atomic_replace (tmp + fsync + os.replace) — never an
    in-place open(path,'w'). A render_fn that raised mid-write would leave the prior map intact;
    here we assert the call routes through atomic_replace (the durability path, DAEMON §4.3)."""
    binding = tmp_path / "binding-ledger.json"
    seen = {"called": False}
    real_atomic = store.atomic_replace

    def spy(path, render_fn):
        seen["called"] = True
        return real_atomic(path, render_fn)

    monkeypatch.setattr(store, "atomic_replace", spy)
    _call(ledger.write_binding, binding_path=binding,
          candidate_map={"a/b#exec": {"state": "running"}}, _lock_held=True)
    assert seen["called"], (
        "write_binding must persist via store.atomic_replace (tmp+fsync+os.replace), "
        "never an in-place write — no torn binding-ledger visible mid-write (DAEMON §4.3)"
    )


def test_binding_cache_tracks_successful_write_and_drops_failed_candidate(
    tmp_path, monkeypatch
):
    """T-19: sanctioned writes are coherent even without a signature change."""
    binding = tmp_path / "binding-ledger.json"
    node = "a/b#exec"
    original = {node: {"node_address": node, "state": "old", "generation": 1}}
    durable_next = {node: {"node_address": node, "state": "new", "generation": 2}}
    failed_candidate = copy.deepcopy(durable_next)
    failed_candidate[node]["state"] = "bad"

    _call(
        ledger.write_binding,
        binding_path=binding,
        candidate_map=original,
        _lock_held=True,
    )
    assert _call(ledger.all_nodes, binding_path=binding) == original
    caller_snapshot = _call(ledger.all_nodes, binding_path=binding)
    caller_snapshot[node]["state"] = "local-only"
    assert _call(ledger.all_nodes, binding_path=binding) == original

    signature = ledger._binding_signature(binding)
    monkeypatch.setattr(ledger, "_binding_signature", lambda _path: signature)
    _call(
        ledger.write_binding,
        binding_path=binding,
        candidate_map=durable_next,
        _lock_held=True,
    )
    assert _call(ledger.all_nodes, binding_path=binding) == durable_next

    real_atomic_replace = store.atomic_replace

    def fail_replace(_path, _render):
        raise RuntimeError("simulated atomic replace failure")

    monkeypatch.setattr(store, "atomic_replace", fail_replace)
    with pytest.raises(RuntimeError, match="simulated atomic replace failure"):
        _call(
            ledger.write_binding,
            binding_path=binding,
            candidate_map=failed_candidate,
            _lock_held=True,
        )
    monkeypatch.setattr(store, "atomic_replace", real_atomic_replace)
    assert _call(ledger.all_nodes, binding_path=binding) == durable_next
