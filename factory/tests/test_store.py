"""Increment 1 Done-test (clause 1 — durable IO + framing + locks): store.atomic_replace,
store.append_framed, store.file_lock, store.normalize_scalars.

Authoritative source: IMPLEMENTATION-PLAN.md §2.1 (frozen store.py interface) +
DAEMON §4.3 (atomic_replace) / §4.4 (framed append) / §4.5 (the lock), transcribed
below as the single source of truth for this suite. The recovered prior art under
research/orchestration-frame/ (save_manifest L191-197, append_ledger L241-246,
control_plane_lock L248-256, normalize_scalars L171-180) is OPTIONAL reference for the
durability discipline; THESE tests are the contract.

Frozen interface under test (§2.1):
    def atomic_replace(path: Path, render_fn: Callable[[IO[str]], None]) -> None
    def file_lock(path: Path, *, shared: bool) -> Iterator[None]   # contextmanager
    def append_framed(path: Path, payload: str) -> None
    def normalize_scalars(obj: Any) -> Any

LOAD-BEARING properties (where "tests pass == correct" lives):
  * atomic_replace: a concurrent reader NEVER sees a partial write; if render_fn raises
    mid-render the target file is UNCHANGED (no truncation); the write goes via a temp
    path + os.replace. FLAG-FOR-MUTANT: an in-place open(path,'w') impl MUST FAIL.
  * append_framed: issues EXACTLY ONE write() per record (the file handle is spied and
    write() calls counted); the frame is "<byte-len>\t<payload>" with byte-len ==
    len(payload.encode()). FLAG-FOR-MUTANT: a two-write (prefix-then-payload) impl MUST
    FAIL the single-write assertion; a wrong/missing byte-len prefix MUST FAIL.
  * file_lock: EX excludes EX; SH allows concurrent SH.

These tests fail RED until harnessd/store.py exists and is correct.
"""

import multiprocessing
import os
import time
from datetime import date, datetime
from pathlib import Path

import pytest

# Direct import (NOT importorskip): until the builder creates harnessd/store.py this is a
# collection-time ImportError, which pytest reports as a RED error for every test in the
# file. A skip would wrongly read as "satisfied".
import harnessd.store as store


# ======================================================================================
# atomic_replace — temp + flush + fsync + os.replace. Durability is the headline.
# ======================================================================================


def test_atomic_replace_writes_content(tmp_path):
    target = tmp_path / "manifest.yaml"
    store.atomic_replace(target, lambda h: h.write("hello: world\n"))
    assert target.read_text() == "hello: world\n"


def test_atomic_replace_overwrites_existing(tmp_path):
    target = tmp_path / "manifest.yaml"
    target.write_text("OLD CONTENTS\n")
    store.atomic_replace(target, lambda h: h.write("NEW CONTENTS\n"))
    assert target.read_text() == "NEW CONTENTS\n"


def test_atomic_replace_render_fn_receives_writable_text_handle(tmp_path):
    target = tmp_path / "out.txt"
    captured = {}

    def render(handle):
        captured["writable"] = handle.writable()
        handle.write("payload")

    store.atomic_replace(target, render)
    assert captured["writable"] is True
    assert target.read_text() == "payload"


def test_atomic_replace_leaves_target_unchanged_when_render_raises(tmp_path):
    """LOAD-BEARING: if render_fn raises mid-render the ORIGINAL target is intact.

    FLAG-FOR-MUTANT: an impl that does ``open(path, 'w')`` and writes in place truncates
    the existing file the instant it opens it, so the original contents are lost the
    moment render_fn raises. That mutant FAILS this assertion. The correct temp-file impl
    never touches the target until os.replace, so the original survives untouched.
    """
    target = tmp_path / "manifest.yaml"
    original = "ORIGINAL DURABLE CONTENTS\n"
    target.write_text(original)

    boom = RuntimeError("render blew up mid-write")

    def render(handle):
        handle.write("HALF-WRITTEN GARBAGE")  # partial bytes that must NOT land
        raise boom

    with pytest.raises(RuntimeError):
        store.atomic_replace(target, render)

    # The target must be byte-for-byte the original — not truncated, not half-written.
    assert target.read_text() == original, (
        "atomic_replace must leave the original target intact when render_fn raises "
        "(an in-place open(path,'w') impl truncates the target and fails here)"
    )


def test_atomic_replace_no_temp_residue_on_success(tmp_path):
    """After a successful replace, no leftover .tmp sibling remains in the dir."""
    target = tmp_path / "manifest.yaml"
    store.atomic_replace(target, lambda h: h.write("ok\n"))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == [], f"unexpected residue after atomic_replace: {leftovers}"


def test_atomic_replace_uses_temp_then_os_replace(tmp_path, monkeypatch):
    """LOAD-BEARING: the write goes through a DISTINCT temp path swapped in by os.replace.

    FLAG-FOR-MUTANT: an in-place ``open(target, 'w')`` impl never calls os.replace and
    never writes to a path other than the target. We spy os.replace and assert it is
    called exactly once with (src != target, dst == target), and that the src is a real
    file at call time. An in-place impl makes no os.replace call -> FAILS.
    """
    target = tmp_path / "manifest.yaml"
    calls = []
    real_replace = os.replace

    def spy_replace(src, dst, *a, **k):
        src_p, dst_p = Path(src), Path(dst)
        calls.append(
            {
                "src": src_p,
                "dst": dst_p,
                "src_existed": src_p.exists(),
                "src_is_target": src_p == target,
            }
        )
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(store.os, "replace", spy_replace)
    store.atomic_replace(target, lambda h: h.write("final\n"))

    assert len(calls) == 1, "atomic_replace must perform exactly one os.replace swap"
    call = calls[0]
    assert call["dst"] == target, "os.replace destination must be the target path"
    assert not call["src_is_target"], "the source must be a TEMP path, not the target"
    assert call["src_existed"], "the temp file must exist (be fully written) before replace"
    assert target.read_text() == "final\n"


def test_atomic_replace_fsyncs_before_replace(tmp_path, monkeypatch):
    """LOAD-BEARING durability ordering: fsync(fileno) happens BEFORE os.replace.

    This is the 'fsync-before-replace is the load-bearing durability line — kept
    verbatim' contract (§2.1 / save_manifest L191-197). We record the order of the two
    syscalls and assert fsync precedes replace. An impl that replaces first (or never
    fsyncs the temp) FAILS.
    """
    target = tmp_path / "manifest.yaml"
    order = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd, *a, **k):
        order.append("fsync")
        return real_fsync(fd, *a, **k)

    def spy_replace(src, dst, *a, **k):
        order.append("replace")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(store.os, "fsync", spy_fsync)
    monkeypatch.setattr(store.os, "replace", spy_replace)
    store.atomic_replace(target, lambda h: h.write("durable\n"))

    assert "fsync" in order, "atomic_replace must fsync the temp file before replacing"
    assert "replace" in order
    assert order.index("fsync") < order.index("replace"), (
        "fsync MUST precede os.replace (load-bearing durability ordering)"
    )


# ======================================================================================
# append_framed — SOLE owner of framing. ONE write() of "<byte-len>\t<payload>...".
# ======================================================================================


class _WriteSpy:
    """Wraps a real text file handle, counting write() calls and recording the chunks.

    Used to prove the single-write-then-fsync contract: append_framed must issue EXACTLY
    ONE write() per record. A prefix-then-payload (two write()) impl is rejected.
    """

    def __init__(self, real_handle):
        self._h = real_handle
        self.writes = []

    def write(self, s):
        self.writes.append(s)
        return self._h.write(s)

    # The store opens the handle with a ``with`` statement, so the spy must support the
    # context-manager protocol. Dunder methods are resolved on the TYPE (not via
    # __getattr__), so they are defined explicitly here and delegate to the real handle.
    def __enter__(self):
        self._h.__enter__()
        return self

    def __exit__(self, *exc):
        return self._h.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._h, name)


def _read_frames(path: Path) -> list[tuple[int, str]]:
    """Parse the on-disk framed file into (declared_len, payload) tuples.

    Frame := "<byte-len>\t<payload>\n". Split on the FIRST tab only; the trailing
    newline is the record terminator and is stripped from the payload.
    """
    frames = []
    raw = path.read_text()
    for line in raw.splitlines():
        if not line:
            continue
        prefix, _, payload = line.partition("\t")
        frames.append((int(prefix), payload))
    return frames


def test_append_framed_roundtrips_single_record(tmp_path):
    target = tmp_path / "run-ledger.jsonl"
    payload = '{"seq": 1, "event": "spawned"}'
    store.append_framed(target, payload)

    frames = _read_frames(target)
    assert len(frames) == 1
    declared_len, parsed_payload = frames[0]
    assert parsed_payload == payload
    assert declared_len == len(payload.encode())


def test_append_framed_prefix_is_byte_length_not_char_length(tmp_path):
    """LOAD-BEARING: the prefix is the BYTE length (encode()), not the char count.

    FLAG-FOR-MUTANT: an impl using len(payload) (code points) instead of
    len(payload.encode()) diverges on any multibyte char. We use an emoji + accented
    text so byte-len != char-len and the wrong impl FAILS.
    """
    target = tmp_path / "run-ledger.jsonl"
    payload = '{"note": "café 🚀 dØne"}'
    assert len(payload) != len(payload.encode()), "fixture must be multibyte to be discriminating"

    store.append_framed(target, payload)
    declared_len, parsed_payload = _read_frames(target)[0]
    assert parsed_payload == payload
    assert declared_len == len(payload.encode()), (
        "the frame prefix MUST be the byte length len(payload.encode()), not the "
        "character count len(payload)"
    )


def test_append_framed_appends_multiple_records_in_order(tmp_path):
    target = tmp_path / "run-ledger.jsonl"
    payloads = ['{"seq": 1}', '{"seq": 2}', '{"seq": 3}']
    for p in payloads:
        store.append_framed(target, p)

    frames = _read_frames(target)
    assert [pl for _, pl in frames] == payloads
    assert [ln for ln, _ in frames] == [len(p.encode()) for p in payloads]


def test_append_framed_issues_exactly_one_write_per_record(tmp_path, monkeypatch):
    """LOAD-BEARING (THE single-write contract): exactly ONE write() per record.

    FLAG-FOR-MUTANT: an impl that does ``handle.write(prefix); handle.write(payload)``
    (or writes the prefix and payload separately) issues TWO write() calls and FAILS
    here. The single write() is what makes 'only the final frame can be torn' true by
    construction (§4.4 torn-tail-safety foundation).
    """
    target = tmp_path / "run-ledger.jsonl"
    spy_holder = {}
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        # Only spy the append-target write handle, not unrelated opens.
        if Path(self) == target and ("a" in (args[0] if args else kwargs.get("mode", "")) or
                                     "w" in (args[0] if args else kwargs.get("mode", ""))):
            spy = _WriteSpy(handle)
            spy_holder["spy"] = spy
            return spy
        return handle

    monkeypatch.setattr(Path, "open", spy_open)
    store.append_framed(target, '{"seq": 1, "event": "x"}')

    assert "spy" in spy_holder, "append_framed must open the target path for appending"
    write_calls = spy_holder["spy"].writes
    assert len(write_calls) == 1, (
        f"append_framed must issue EXACTLY ONE write() per record, saw "
        f"{len(write_calls)}: {write_calls!r}. A two-write (prefix-then-payload) impl "
        "fails this single-write contract."
    )
    # The single write must carry the whole frame: prefix, tab, payload.
    (whole,) = write_calls
    assert "\t" in whole
    prefix, _, rest = whole.partition("\t")
    assert prefix.isdigit()
    assert rest.startswith('{"seq": 1, "event": "x"}')


def test_append_framed_fsyncs_after_write(tmp_path, monkeypatch):
    """The single write() is followed by an fsync (single-write-THEN-fsync)."""
    target = tmp_path / "run-ledger.jsonl"
    fsync_calls = []
    real_fsync = os.fsync

    def spy_fsync(fd, *a, **k):
        fsync_calls.append(fd)
        return real_fsync(fd, *a, **k)

    monkeypatch.setattr(store.os, "fsync", spy_fsync)
    store.append_framed(target, '{"seq": 1}')
    assert len(fsync_calls) >= 1, "append_framed must fsync after the write (durability)"


def test_append_framed_payload_with_tab_keeps_prefix_authoritative(tmp_path):
    """A payload containing a TAB must still round-trip: split on the FIRST tab only,
    and the byte-len prefix remains the authority over the full payload.
    """
    target = tmp_path / "run-ledger.jsonl"
    payload = '{"text": "a\\tb"}'  # the JSON-escaped backslash-t (a literal backslash + t)
    store.append_framed(target, payload)
    declared_len, parsed_payload = _read_frames(target)[0]
    assert parsed_payload == payload
    assert declared_len == len(payload.encode())


# ======================================================================================
# normalize_scalars — PORT (L171-180): canonicalize datetime/date to isoformat, recurse.
# ======================================================================================


def test_normalize_scalars_passes_through_plain_scalars():
    assert store.normalize_scalars("s") == "s"
    assert store.normalize_scalars(7) == 7
    assert store.normalize_scalars(3.5) == 3.5
    assert store.normalize_scalars(True) is True
    assert store.normalize_scalars(None) is None


def test_normalize_scalars_canonicalizes_datetime_to_isoformat():
    dt = datetime(2026, 6, 5, 12, 0, 0)
    assert store.normalize_scalars(dt) == dt.isoformat()


def test_normalize_scalars_canonicalizes_date_to_isoformat():
    d = date(2026, 6, 5)
    assert store.normalize_scalars(d) == d.isoformat()


def test_normalize_scalars_recurses_into_dicts_and_lists():
    dt = datetime(2026, 6, 5, 12, 0, 0)
    obj = {"a": [dt, "x"], "b": {"c": dt}}
    out = store.normalize_scalars(obj)
    assert out == {"a": [dt.isoformat(), "x"], "b": {"c": dt.isoformat()}}


def test_normalize_scalars_output_is_json_serializable():
    import json

    obj = {"when": datetime(2026, 6, 5, 12, 0, 0), "day": date(2026, 6, 5), "n": 1}
    # Round-trips through json without a custom encoder (the point of normalization).
    json.dumps(store.normalize_scalars(obj))


# ======================================================================================
# file_lock — fcntl SH (shared) / EX (exclusive) contextmanager. Mutual exclusion.
# ======================================================================================


def test_file_lock_is_a_contextmanager(tmp_path):
    lock_path = tmp_path / ".control-plane.lock"
    with store.file_lock(lock_path, shared=False):
        pass  # acquires and releases without error


def test_file_lock_releases_on_exit_so_reacquire_succeeds(tmp_path):
    """After the EX context exits, the lock is released — a re-acquire in the SAME
    process succeeds immediately (LOCK_UN ran in finally).
    """
    lock_path = tmp_path / ".control-plane.lock"
    with store.file_lock(lock_path, shared=False):
        pass
    # Re-acquire must not block/deadlock.
    with store.file_lock(lock_path, shared=False):
        pass


def test_file_lock_releases_even_if_body_raises(tmp_path):
    lock_path = tmp_path / ".control-plane.lock"
    with pytest.raises(ValueError):
        with store.file_lock(lock_path, shared=False):
            raise ValueError("body blew up")
    # Lock must have been released in finally; re-acquire succeeds.
    with store.file_lock(lock_path, shared=False):
        pass


# ---- Cross-process exclusion (the real fcntl semantics, not same-fd no-ops) ----------


def _hold_ex_lock(lock_path_str, acquired_evt, release_evt):
    """Child: acquire EX, signal acquired, hold until told to release."""
    import harnessd.store as child_store

    with child_store.file_lock(Path(lock_path_str), shared=False):
        acquired_evt.set()
        release_evt.wait(timeout=10)


def _hold_sh_lock(lock_path_str, acquired_evt, release_evt):
    import harnessd.store as child_store

    with child_store.file_lock(Path(lock_path_str), shared=True):
        acquired_evt.set()
        release_evt.wait(timeout=10)


def _probe_file_lock(lock_path_str, shared, got_it_evt):
    """Child: try to acquire store.file_lock(shared=shared) and, IF granted, set the
    event. If the acquire BLOCKS (another process holds an incompatible lock) the event
    is never set and the parent observes the timeout as 'refused'.

    Crucially this probe drives store.file_lock ITSELF, so the SH-vs-EX compatibility it
    observes is exactly the lock mode store.file_lock requested — not a raw fcntl call
    that would mask an EX->SH downgrade in the implementation under test.
    """
    import harnessd.store as child_store

    with child_store.file_lock(Path(lock_path_str), shared=shared):
        got_it_evt.set()
        # Release immediately; we only care whether the acquire was granted.


def _probe_grants(ctx, lock_path, *, shared, timeout=2.0) -> bool:
    """Run _probe_file_lock in a child and report whether the acquire was GRANTED.

    A blocking flock has no portable NB hook through the public store.file_lock API, so
    we detect 'refused' as 'the child did not acquire within the timeout while a holder
    is active'. A granted acquire sets the event near-instantly; a refused one blocks, so
    the moment the wait window lapses we terminate the still-blocked probe and report
    refusal. The timeout only bounds the REFUSED case — the granted case returns at once.
    """
    got_it = ctx.Event()
    proc = ctx.Process(target=_probe_file_lock, args=(str(lock_path), shared, got_it))
    proc.start()
    granted = got_it.wait(timeout=timeout)
    if not granted and proc.is_alive():  # still blocked on the lock — terminate the probe
        proc.terminate()
    proc.join(timeout=timeout + 2)
    return bool(granted)


@pytest.fixture
def _spawn_ctx():
    # 'fork' keeps the child cheap and inherits sys.path/conftest wiring; on macOS the
    # default is 'spawn' which also works since harnessd is importable via installed path.
    return multiprocessing.get_context("fork")


def test_file_lock_ex_excludes_other_acquire_across_processes(tmp_path, _spawn_ctx):
    """LOAD-BEARING: while one process holds file_lock(EX), an incompatible acquire
    through file_lock CANNOT be granted.

    The discriminating probe requests SHARED: an EXCLUSIVE hold must REFUSE a shared
    acquire, whereas a (wrong) shared hold would ADMIT it. So probing with shared=True
    against an exclusive hold is exactly the assertion that an EX->SH downgrade mutant
    fails. The probe drives store.file_lock itself, so the lock mode actually requested
    by the implementation is what is under test.
    """
    lock_path = tmp_path / ".control-plane.lock"
    lock_path.touch()
    acquired = _spawn_ctx.Event()
    release = _spawn_ctx.Event()
    holder = _spawn_ctx.Process(
        target=_hold_ex_lock, args=(str(lock_path), acquired, release)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=10), "holder failed to acquire EX lock"
        # An EXCLUSIVE hold must refuse a SHARED acquire (and a fortiori an EX one).
        # If file_lock(EX) were a no-op or were downgraded to SH, this SH probe would be
        # GRANTED and the assertion fails.
        granted = _probe_grants(_spawn_ctx, lock_path, shared=True)
        assert granted is False, (
            "file_lock(shared=False) must hold a real EXCLUSIVE lock that REFUSES a "
            "concurrent shared acquire; an EX->SH downgrade (or no-op) is admitted here "
            "and fails"
        )
    finally:
        release.set()
        holder.join(timeout=10)

    # After the holder releases, an acquire is granted again.
    assert _probe_grants(_spawn_ctx, lock_path, shared=False) is True


def test_file_lock_sh_allows_concurrent_sh_across_processes(tmp_path, _spawn_ctx):
    """LOAD-BEARING: shared locks coexist — while one process holds file_lock(SH),
    another file_lock(SH) acquire is GRANTED (readers don't block readers).
    """
    lock_path = tmp_path / ".control-plane.lock"
    lock_path.touch()
    acquired = _spawn_ctx.Event()
    release = _spawn_ctx.Event()
    holder = _spawn_ctx.Process(
        target=_hold_sh_lock, args=(str(lock_path), acquired, release)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=10), "holder failed to acquire SH lock"
        # A concurrent SHARED acquire must be granted while the holder holds SH.
        granted = _probe_grants(_spawn_ctx, lock_path, shared=True)
        assert granted is True, (
            "file_lock(shared=True) must ALLOW a concurrent shared acquire (readers "
            "coexist); an SH->EX upgrade mutant would refuse it and fails here"
        )
    finally:
        release.set()
        holder.join(timeout=10)
