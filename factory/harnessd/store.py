"""Atomic-IO + lock primitives — the durable-write floor the whole harness writes through.

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.1 (frozen store.py interface) + §1 module table (store.py row).
  - DAEMON §4.3 (atomic_replace), §4.4 (framed single-write+fsync append), §4.5 (the lock).
  - Recovered prior art (research/orchestration-frame/self-improvement-harness/control_plane.py):
    save_manifest L191-197 (PORT — the tmp + flush + fsync-BEFORE-replace durability line is
    kept verbatim), append_ledger L241-246 (PORT — the fsync discipline; framing is NEW here),
    control_plane_lock L248-256 (PORT — fcntl SH/EX, LOCK_UN in finally), normalize_scalars
    L171-180 (PORT — datetime/date -> isoformat, recurse into dicts/lists).

LOAD-BEARING contracts (where "tests pass == correct" lives):
  * atomic_replace: writes a DISTINCT temp sibling, fsync's it, then ``os.replace`` swaps it
    in. The target is never opened in place, so a render_fn that raises mid-write leaves the
    original byte-for-byte intact. fsync MUST precede os.replace (durability ordering).
  * append_framed: SOLE owner of framing. ONE framed record == EXACTLY ONE ``write()`` of
    ``f"{len(payload.encode())}\t{payload}\n"`` + flush + ONE fsync. The ``<byte-len>`` prefix
    is computed over the exact UTF-8 bytes and is the sole torn-tail authority (no in-payload
    length). The single write() is what makes "only the FINAL line can be torn" true by
    construction.
  * file_lock: fcntl SH (shared) / EX (exclusive); LOCK_UN in finally so the lock always
    releases — even when the body raises.
  * flock_exclusive_nb: the PERSISTENT-hold counterpart (LOCK_EX|LOCK_NB, returns the OPEN
    handle the caller keeps for its lifetime); BlockingIOError propagates when another holder
    exists. Used by the daemon's single-instance guard (DAEMON §2.3).

NOTE: ``os.replace`` and ``os.fsync`` are invoked through the module-level ``os`` reference on
purpose — the durability-ordering tests monkeypatch ``store.os.fsync`` / ``store.os.replace``
to observe the syscall order. ``from os import replace`` would defeat that spy.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import IO, Any, Callable, Iterator


def atomic_replace(path: Path, render_fn: Callable[[IO[str]], None]) -> None:
    """Durably replace ``path`` with whatever ``render_fn`` writes — all-or-nothing.

    The render goes to a DISTINCT temp sibling (``.{name}.tmp``); the temp is flushed and
    fsync'd, and only then is ``os.replace`` used to atomically swap it onto ``path``. A
    concurrent reader therefore NEVER sees a partial write, and if ``render_fn`` raises
    mid-render the original ``path`` is untouched (the temp is discarded, the target was
    never opened in place).

    PORT of save_manifest L191-197 — the fsync-before-replace is the load-bearing durability
    line, kept verbatim.

    Parent-directory fsync is intentionally omitted (matches the save_manifest prior art): a
    rename lost to power-loss is recovered by WAL replay (§4.4/§5.1), which is the durability
    backstop for this primitive. The parent dir is created here if missing (symmetry with the
    other write primitives), though genesis establishes the runtime tree before any commit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            render_fn(handle)
            handle.flush()
            os.fsync(handle.fileno())  # durability: temp is on disk BEFORE the swap
        os.replace(tmp, path)  # atomic swap — fsync above MUST precede this
    except BaseException:
        # render_fn (or the write) blew up: discard the partial temp so no .tmp residue is
        # left behind and the original target survives untouched.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


@contextmanager
def file_lock(path: Path, *, shared: bool) -> Iterator[None]:
    """Hold an fcntl lock on ``path`` for the duration of the ``with`` block.

    ``shared=False`` takes an EXCLUSIVE (LOCK_EX) lock; ``shared=True`` takes a SHARED
    (LOCK_SH) lock. EX excludes EX and SH; SH coexists with SH (readers don't block
    readers). LOCK_UN runs in ``finally`` so the lock is always released — even if the body
    raises. In the daemon this is taken ONCE by the daemon process, not per-CLI-call.

    PORT of control_plane_lock L248-256.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def flock_exclusive_nb(path: Path) -> IO[str]:
    """Acquire LOCK_EX|LOCK_NB on ``path`` and return the OPEN handle — the PERSISTENT-hold primitive.

    The counterpart to the scoped ``file_lock`` context manager: the caller KEEPS the returned
    handle for its lifetime, and the flock dies with the fd/process (there is no scoped release —
    closing the handle is the release). NON-BLOCKING: when another holder exists, the OSError
    (``BlockingIOError`` for EWOULDBLOCK) PROPAGATES — fail-loud, never wait — and the handle is
    closed before re-raising (no leaked fd). NB: flock conflicts across distinct open file
    descriptions even within ONE process, so a second call on the same path from the same process
    also raises (verified on darwin).

    Policy-free on purpose (this module is the lock-primitive home): the single-instance semantics
    (which path, the refusal message, the lifetime stash) live in the caller — the daemon's §2.3
    instance guard on ``.harnessd.instance.lock``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()  # no holder-less fd leak — the caller gets the raise, not a dead handle
        raise
    return handle


def append_framed(path: Path, payload: str) -> None:
    """Append ONE length-framed record to ``path`` with a single durable write().

    The frame is ``f"{len(payload.encode())}\\t{payload}\\n"``: a byte-length prefix, a TAB,
    the RAW payload, and a record-terminating newline. This function is the SOLE owner of
    framing — callers hand it the raw json string, never a pre-framed one.

    The whole frame is emitted in EXACTLY ONE ``write()`` syscall, then flushed and fsync'd.
    That single write is what makes "only the FINAL line can ever be torn" true by
    construction. PRECONDITION (enforced below): ``payload`` MUST NOT contain a raw newline.
    The on-disk record is ONE physical line, and the ``<byte-len>`` prefix (computed over the
    exact UTF-8 bytes) validates that single line — it is the torn-tail authority *for a
    line-oriented reader* (``load_wal`` reads line-by-line, §2.4), not a length frame that
    spans lines. A raw newline in the payload would split one record across physical lines and
    trip a FALSE corruption-halt on boot recovery, so it is rejected here, loud. The only
    intended caller is ``ledger.append_wal``, which hands ``json.dumps(record)`` with the
    default ``ensure_ascii=True`` — that never emits a raw newline, so the contract always holds.

    PORT of the append_ledger L241-246 fsync discipline; the ``<len>\\t`` frame is NEW.
    """
    if "\n" in payload:
        raise ValueError(
            "append_framed payload must not contain a raw newline "
            "(one record = one physical line; a newline would split the record and "
            "trigger a false torn-tail corruption-halt in load_wal)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    byte_len = len(payload.encode("utf-8"))
    frame = f"{byte_len}\t{payload}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(frame)  # EXACTLY ONE write() per record (the single-write contract)
        handle.flush()
        os.fsync(handle.fileno())  # durability: the record is on disk before we return


def normalize_scalars(obj: Any) -> Any:
    """Canonicalize datetime/date scalars to ISO-8601 strings, recursing into dicts/lists.

    Leaves plain scalars (str/int/float/bool/None) untouched. The point is to make a nested
    structure json-serializable without a custom encoder. PORT of normalize_scalars L171-180.
    """
    if isinstance(obj, dict):
        return {key: normalize_scalars(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [normalize_scalars(item) for item in obj]
    # NB: datetime is a subclass of date — check datetime first so a datetime isn't matched
    # by the date branch and truncated to a date-only isoformat.
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    return obj
