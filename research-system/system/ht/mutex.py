"""Root-scoped serialization for merge and globally allocated ledger writes."""

from __future__ import annotations

import errno
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import HtError
from .paths import Root


@contextmanager
def global_mutex(
    root: Root,
    *,
    timeout: float = 1.0,
    poll_interval: float = 0.01,
) -> Iterator[None]:
    """Hold the root's global merge/ledger mutex for one critical section.

    Ledger ID allocation must occur inside this critical section: scanning
    outside it lets simultaneous creates in different books mint the same
    union-global ID.
    """

    if timeout < 0:
        raise ValueError("timeout must be non-negative")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")

    lock_path = root.global_lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HtError(
                        "global merge/ledger mutex is contended "
                        "(coherence amendments §3)"
                    ) from exc
                time.sleep(min(poll_interval, remaining))

        yield
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
