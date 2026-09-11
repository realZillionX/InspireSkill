"""Run shared SSH/SCP command lines with native asyncio subprocess I/O.

Timeout and cancellation kill and reap the local child, including cancellation
during spawn. That is a local cleanup guarantee, not proof that a command on the
remote host stopped or that an interrupted transfer rolled back.

Streaming drains both pipes and serializes callbacks through a bounded queue.
Slow callbacks exert backpressure; separate stdout/stderr pipes do not establish
a global remote write order. Merged CLI output retains line delivery, so one
unterminated line can grow even though the chunk queue is bounded.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import subprocess
import sys
from typing import Any, Callable

from inspire.platform.errors import ConfigurationError


async def _finish(task: asyncio.Task[Any]) -> Any:
    """Reap local children even when cancellation is repeated during cleanup."""
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


async def _spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    task = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        return await asyncio.shield(task)
    except NotImplementedError as error:
        if sys.platform != "win32":
            raise
        raise ConfigurationError(
            "Async SSH execution and transfer on Windows require a ProactorEventLoop; "
            "the current event loop does not support subprocesses. Configure your framework "
            "to use ProactorEventLoop (or WindowsProactorEventLoopPolicy before starting "
            "the loop), then create the async client inside that loop."
        ) from error
    except asyncio.CancelledError:

        async def finish_spawn() -> None:
            process = await task
            await _reap(process)

        await _finish(asyncio.create_task(finish_spawn()))
        raise


async def _reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def run_process(
    args: list[str],
    *,
    timeout: float | None = None,
    capture_output: bool = True,
    text: bool = True,
    encoding: str = "utf-8",
    errors: str = "replace",
    env: Any = None,
    stdin: Any = None,
) -> subprocess.CompletedProcess:
    # Accepts what the shared command lines pass to subprocess.run: a caller that
    # detaches the child's stdin must keep doing so when the driver runs natively.
    process = await _spawn(
        *args,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE if capture_output else None,
        stderr=asyncio.subprocess.PIPE if capture_output else None,
        env=env,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError as error:
            raise subprocess.TimeoutExpired(args, timeout or 0) from error
        assert process.returncode is not None
        return subprocess.CompletedProcess(
            args,
            process.returncode,
            stdout.decode(encoding, errors) if text and stdout is not None else stdout,
            stderr.decode(encoding, errors) if text and stderr is not None else stderr,
        )
    finally:
        await _finish(asyncio.create_task(_reap(process)))


async def stream_process(
    args: list[str],
    script: str | None,
    output_callback: Callable[..., Any],
    stderr_callback: Callable[..., Any] | None,
    timeout: float | None,
    env: Any,
    bridge_name: str,
    *,
    deliver: Callable[..., Any],
) -> int:
    process = await _spawn(
        *args,
        stdin=asyncio.subprocess.PIPE if script is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
        if stderr_callback is not None
        else asyncio.subprocess.STDOUT,
        env=env,
    )
    # One consumer preserves callback serialization and bounded backpressure.
    pending: asyncio.Queue[tuple[int, bytes | None]] = asyncio.Queue(maxsize=16)
    streams = [process.stdout] + ([process.stderr] if stderr_callback is not None else [])
    callbacks = [output_callback, stderr_callback]
    decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in streams]

    async def read(index: int, stream: Any) -> None:
        try:
            while data := await stream.read(4096):
                await pending.put((index, data))
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            # A child that exits while its pipe is being drained tears the pipe
            # down. That is the end of the stream, not a failure the caller can
            # act on; the exit status still decides whether the command worked.
            pass
        await pending.put((index, None))

    readers = [asyncio.create_task(read(i, stream)) for i, stream in enumerate(streams)]

    async def consume() -> int:
        if script is not None and process.stdin is not None:
            # A command that fails before reading its script closes stdin first.
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                process.stdin.write(script.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
        ended = 0
        line = ""
        while ended < len(streams):
            index, data = await pending.get()
            chunk = decoders[index].decode(data or b"", final=data is None)
            if stderr_callback is None:
                # The merged CLI stream historically emits complete lines.
                line += chunk
                while "\n" in line:
                    head, line = line.split("\n", 1)
                    await deliver(output_callback, head + "\n")
                if data is None and line:
                    await deliver(output_callback, line)
            elif chunk:
                await deliver(callbacks[index], chunk)
            if data is None:
                ended += 1
        return await process.wait()

    try:
        task = asyncio.create_task(consume())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise subprocess.TimeoutExpired(args, timeout or 0)
        return task.result()
    finally:

        async def cleanup() -> None:
            task.cancel()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(task, *readers, return_exceptions=True)
            await _reap(process)

        await _finish(asyncio.create_task(cleanup()))
