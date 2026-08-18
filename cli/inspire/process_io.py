"""Portable line reading from a live subprocess pipe.

``select`` accepts only sockets on Windows, so the loop every streaming SSH
caller wants — read a line if one is ready, otherwise get control back to check
a timeout or poll a status endpoint — needs a reader thread there. This module
hides that split behind one generator so callers never branch on the platform.
"""

from __future__ import annotations

import queue
import select
import subprocess
import sys
import threading
from collections.abc import Iterator
from typing import IO

_QUEUE_SENTINEL = object()


def iter_process_lines(
    process: subprocess.Popen,
    stream: IO[str],
    *,
    poll_interval: float = 1.0,
) -> Iterator[str | None]:
    """Yield lines from *stream* until the process exits and the pipe drains.

    Yields ``None`` whenever *poll_interval* passes with nothing to read, so a
    caller can do periodic work (enforce a timeout, poll job status) from a
    single loop. Lines keep their trailing newline, exactly as ``readline``
    returns them.
    """
    if sys.platform == "win32":
        yield from _iter_lines_via_thread(stream, poll_interval=poll_interval)
        return
    yield from _iter_lines_via_select(process, stream, poll_interval=poll_interval)


def _iter_lines_via_select(
    process: subprocess.Popen,
    stream: IO[str],
    *,
    poll_interval: float,
) -> Iterator[str | None]:
    while True:
        # Read from a single path (readline only) so lines cannot be emitted twice.
        if process.poll() is not None:
            line = stream.readline()
        else:
            ready, _, _ = select.select([stream], [], [], poll_interval)
            if not ready:
                yield None
                continue
            line = stream.readline()
        if not line:
            return
        yield line


def _iter_lines_via_thread(stream: IO[str], *, poll_interval: float) -> Iterator[str | None]:
    pending: queue.Queue[object] = queue.Queue()

    def drain() -> None:
        try:
            for line in iter(stream.readline, ""):
                pending.put(line)
        except (OSError, ValueError):
            # The pipe was closed underneath us — treat it as end of stream.
            pass
        finally:
            pending.put(_QUEUE_SENTINEL)

    reader = threading.Thread(target=drain, daemon=True, name="inspire-pipe-reader")
    reader.start()
    try:
        while True:
            try:
                item = pending.get(timeout=poll_interval)
            except queue.Empty:
                yield None
                continue
            if item is _QUEUE_SENTINEL:
                return
            yield str(item)
    finally:
        # The thread is blocked in readline; it exits once the caller closes the
        # pipe or reaps the process. Daemon status keeps it from holding exit.
        reader.join(timeout=1.0)


__all__ = ["iter_process_lines"]
