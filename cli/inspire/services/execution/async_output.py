"""Keep synchronous output sinks usable from native async exec paths.

File and caller-owned sink writes use inspire.platform.web.offload. Each write
finishes before cancellation propagates, so cleanup cannot close a sink beneath
an unfinished write. A slow sink can therefore delay cancellation. Capture limits
still belong to inspire.exec_output; writing a sink does not enlarge capture.

on_output callbacks are different: deliver_output invokes them on the caller
loop and awaits any awaitable result, preserving order and backpressure. A slow
synchronous callback still blocks that loop.
"""
from __future__ import annotations

from inspire.platform.web.offload import offload

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from inspire.exec_output import OutputTarget, output_writer


class AsyncOutputWriter:
    def __init__(self, writer: Any):
        self.writer = writer

    async def write(self, text: str) -> None:
        await _finish_io(self.writer.write, text)


@asynccontextmanager
async def async_output_writer(target: OutputTarget) -> AsyncIterator[AsyncOutputWriter | None]:
    context = output_writer(target)
    try:
        writer = await _finish_io(context.__enter__) if target is not None else None
        yield AsyncOutputWriter(writer) if writer is not None else None
    finally:
        if target is not None:
            await _finish_io(context.__exit__, None, None, None)


async def _finish_io(function: Callable[..., Any], *args: Any) -> Any:
    task = asyncio.create_task(offload(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        task.exception()
        raise asyncio.CancelledError
    return task.result()


async def deliver_output(callback: Callable[[str], Any], chunk: str) -> None:
    """Deliver user or internal callbacks in order, awaiting backpressure if supplied."""
    result = callback(chunk)
    if inspect.isawaitable(result):
        await result
