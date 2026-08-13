"""Cross-process exclusive locks for shared ``~/.inspire`` state.

Several CLI processes run at once — most visibly the OpenSSH ``ProxyCommand``
invocations one ``ssh`` spawns. The shared JSON caches are read-modify-write,
so concurrent writers must serialize or silently drop each other's entries.

The lock lives in a sibling ``<name>.lock`` file so the guarded payload can
still be replaced atomically, and ``flock`` is released by the kernel when the
holder exits, so a crashed writer never strands it.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_POLL_INTERVAL = 0.05

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class CacheLockTimeout(TimeoutError):
    """An exclusive cache lock was still held when the caller's deadline passed."""


@contextmanager
def exclusive_cache_lock(
    cache_path: Path,
    *,
    timeout: float | None = None,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Hold an exclusive lock covering *cache_path* for the block's duration.

    Waits indefinitely by default. Pass *timeout* (seconds) to bound the wait
    and raise :class:`CacheLockTimeout` instead — worth doing wherever the lock
    guards a long operation and hanging forever behind a stuck holder is worse
    than proceeding unserialized. *on_wait* runs once, before blocking, when
    the lock is contended, so callers can explain the pause.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_name(f"{cache_path.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire(descriptor, lock_path, timeout=timeout, on_wait=on_wait)
        try:
            yield
        finally:
            _release(descriptor)
    finally:
        os.close(descriptor)


def _acquire(
    descriptor: int,
    lock_path: Path,
    *,
    timeout: float | None,
    on_wait: Callable[[], None] | None,
) -> None:
    if timeout is None and on_wait is None:
        _block_acquire(descriptor)
        return

    if _try_acquire(descriptor):
        return
    if on_wait is not None:
        on_wait()
    if timeout is None:
        _block_acquire(descriptor)
        return

    deadline = time.monotonic() + timeout
    while True:
        if _try_acquire(descriptor):
            return
        if time.monotonic() >= deadline:
            raise CacheLockTimeout(f"Timed out waiting for {lock_path}")
        time.sleep(_POLL_INTERVAL)


def _try_acquire(descriptor: int) -> bool:
    if sys.platform == "win32":
        # msvcrt locks from the current file position. Keep a single byte at
        # offset zero reserved for the lock's whole lifetime.
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise
    return True


def _block_acquire(descriptor: int) -> None:
    if sys.platform != "win32":
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    while not _try_acquire(descriptor):
        time.sleep(_POLL_INTERVAL)


def _release(descriptor: int) -> None:
    if sys.platform != "win32":
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
