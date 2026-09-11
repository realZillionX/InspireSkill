"""Offload blocking preparation without moving SDK business logic into workers.

Each async SDK client owns up to four lazy worker threads, independent of native
network concurrency. They handle local files, cache locks, request preparation
and output sinks; SSH reachability and retry waits use native async I/O.
One bootstrap exception remains: _ensure_rtunnel_binary checks the local binary
and may download a missing/unusable rtunnel release in a worker.
Standalone async callers without current_pool use asyncio.to_thread instead.

Cancelling a waiter cannot interrupt a running Python function. The pool keeps
its concurrent futures so client shutdown can join them before releasing state;
shutdown may therefore outlast a cancelled operation's timeout.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial
from typing import Any, Callable


LOCAL_IO_WORKERS = 4


class OffloadPool:
    """Bound worker count, not submissions; queued work can exceed four calls."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=LOCAL_IO_WORKERS, thread_name_prefix="inspire-local-io",
        )
        self._pending: set[Future[Any]] = set()

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # Keep the concurrent future even when its asyncio waiter is cancelled:
        # close must still join work that cannot be forcibly interrupted.
        self._pending = {future for future in self._pending if not future.done()}
        future = self._executor.submit(copy_context().run, partial(function, *args, **kwargs))
        self._pending.add(future)
        return await asyncio.wrap_future(future)

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        try:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in self._pending),
                return_exceptions=True,
            )
        finally:
            # All running functions have returned; joining no longer blocks I/O.
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._pending.clear()


current_pool: ContextVar[OffloadPool | None] = ContextVar("inspire_offload_pool", default=None)


async def offload(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    pool = current_pool.get()
    if pool is None:
        # Standalone async transport/CLI users retain their existing behavior.
        return await asyncio.to_thread(function, *args, **kwargs)
    return await pool.run(function, *args, **kwargs)
