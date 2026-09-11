"""Share SDK business logic on the caller loop, suspending at explicit I/O calls.

The first use pins account and configuration. Each operation gets a shallow
client view with its own facade bindings, deadline and write state; catalogue
storage, the plaza sign-in slot and acquired authentication snapshots are shared.
Views are not thread workers or leased synchronous clients. Facade-local caches are rebuilt
with each view, including the notebook contents-root cache.

concurrency is a deprecated, validated no-op. Native requests overlap freely;
Ray/Serving bulk status uses a separate eight-task rolling window. Local I/O
uses the client-owned pool in inspire.platform.web.offload. HTTP connections
also live for the client lifetime. Operation views release their own resources
without closing shared sessions. Close cancels active operations, closes HTTP
connections on the owning loop and joins pending offloads; it does not stop
remote workloads.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from copy import copy
import os
import inspect
import threading
import time
from typing import Any
import warnings


from inspire.platform.web.flow import async_call, call, run_sync as _run_sync
from .client import InspireClient
from .models import ResourceRef
from .exceptions import ClientClosedError, ClientThreadError, ValidationError

from inspire.platform.web.offload import OffloadPool, current_pool

STATUS_CONCURRENCY = 8


async def _finish(future: asyncio.Future[Any]) -> Any:
    """Finish cleanup even if the owner is cancelled more than once."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        future.exception()
        raise asyncio.CancelledError
    return future.result()




class AsyncRuntime:
    def __init__(self, options: dict[str, Any], concurrency: int | None) -> None:
        from inspire.platform.web.transport_async import AsyncClientPool

        if concurrency is not None:
            if type(concurrency) is not int or concurrency < 1:
                raise ValidationError("concurrency must be a positive integer.")
            warnings.warn(
                "concurrency is deprecated and has no effect; async requests run natively.",
                DeprecationWarning, stacklevel=3,
            )
        self._offload_pool = OffloadPool()
        self._http_pool = AsyncClientPool()
        self._options = options
        self._pid = os.getpid()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: InspireClient | None = None
        self._active: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._closing: asyncio.Task[None] | None = None
        self._account: str | None = None
        self._base_url: str | None = None

    def _check(self) -> None:
        loop = asyncio.get_running_loop()
        if self._pid != os.getpid() or self._loop not in (None, loop):
            raise ClientThreadError("Use each async Client in the same process and event loop.")
        self._loop = loop

    async def _start(self) -> None:
        self._check()
        if self._closed:
            raise ClientClosedError("Client is closed.")
        if self._client is None:
            try:
                # Local configuration only; construction never authenticates.
                self._client = InspireClient(**self._options)
                self._account, self._base_url = self._client.account, self._client.base_url
            except BaseException:
                self._closed = True
                if self._client is not None:
                    self._client.close()
                raise
            finally:
                self._options = {}

    def _operation_client(self) -> InspireClient:
        assert self._client is not None
        # Lightweight call-local state, never a pool/lease or another account read.
        # Facade callbacks must bind to this view; the catalog cache is shared.
        client = copy(self._client)
        client._transport = copy(self._client._transport)
        # Copy the write claim, retaining the shared authentication evidence.
        client._transport._decisions = copy(self._client._transport._decisions)
        client._catalog_context = None
        for name, value in vars(self._client).items():
            if not name.startswith("_") and name != "cache" and hasattr(value, "client"):
                setattr(client, name, type(value)(client))
        return client

    async def _invoke(self, facade: str, method: str, args: tuple[Any, ...],
                      kwargs: dict[str, Any], producer: Callable[[Any], Any] | None = None,
                      deadline: float | None = None) -> Any:
        from inspire.platform.web.transport_async import AsyncDriver, current_client_pool

        callback = kwargs.get("on_output")
        if callback is not None and producer is None:
            def output(chunk: str) -> Any:
                value = callback(chunk)
                bridge = async_call.get()
                if inspect.isawaitable(value) and bridge is not None:
                    return bridge(call(_await_output, value))
                return value
            kwargs = dict(kwargs, on_output=output)
        client = self._operation_client()
        token = current_pool.set(self._offload_pool)
        http_token = current_client_pool.set(self._http_pool)
        client._transport.deadline = deadline
        try:
            async with AsyncDriver(client._transport) as driver:
                target = getattr(client, facade) if facade else client
                bound = getattr(target, method)
                return await _run_sync(driver, lambda: producer(bound) if producer else bound(*args, **kwargs))
        finally:
            assert self._client is not None
            owner, current = self._client._transport, client._transport
            if current._session is not None and (
                owner._session is None or current._session.created_at >= owner._session.created_at
            ):
                owner._session = current._session
            try:
                current.release_view(owner)
            finally:
                current_client_pool.reset(http_token)
                current_pool.reset(token)

    async def _tracked(self, coroutine: Any) -> Any:
        task = asyncio.create_task(coroutine)
        self._active.add(task)
        try:
            return await task
        finally:
            self._active.discard(task)

    async def _status(self, facade: str, refs: Any, workspace: Any) -> tuple[Any, ...]:
        # A rolling window bounds both sockets and tasks. Await input order so a
        # later fast failure cannot replace the first reference's exception.
        if isinstance(refs, str):
            raise ValidationError("refs must be a sequence, not a string.")
        pending: list[asyncio.Task[Any]] = []
        iterator = iter(refs)
        assert self._client is not None
        deadline = time.monotonic() + self._client.operation_timeout

        def schedule() -> None:
            for ref in iterator:
                pending.append(asyncio.create_task(self._invoke(
                    facade, "get", (), {"ref": ref, "workspace": workspace}, deadline=deadline,
                )))
                break

        results = []
        try:
            for _ in range(STATUS_CONCURRENCY):
                schedule()
            while pending:
                results.append(await pending[0])
                pending.pop(0)
                schedule()
            return tuple(results)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _validate_binding(self, ref: ResourceRef, kind: type[ResourceRef]) -> None:
        await self._start()
        assert self._client is not None
        self._client._validate_ref(ref, kind)

    async def _call(self, facade: str, method: str, *args: Any, **kwargs: Any) -> Any:
        await self._start()
        if facade in {"ray", "servings"} and method == "status":
            return await self._tracked(self._status(
                facade, kwargs["refs"], kwargs.get("workspace"),
            ))
        return await self._tracked(self._invoke(facade, method, args, kwargs))

    async def _stream(self, facade: str, method: str, *args: Any,
                      output: bool = False, **kwargs: Any) -> AsyncGenerator[Any, None]:
        await self._start()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        loop = asyncio.get_running_loop()
        thread = threading.get_ident()
        stopped = threading.Event()

        def emit(value: Any) -> Any:
            if stopped.is_set():
                raise asyncio.CancelledError
            bridge = async_call.get()
            if threading.get_ident() != thread:
                pending = asyncio.run_coroutine_threadsafe(queue.put(value), loop)
                try:
                    while not stopped.is_set():
                        try:
                            return pending.result(timeout=0.05)
                        except TimeoutError:
                            pass
                    raise asyncio.CancelledError
                finally:
                    pending.cancel()
            if bridge is not None:
                return bridge(call(queue.put, value))
            return queue.put(value)

        def produce(bound: Any) -> None:
            if output:
                callback = kwargs.get("on_output")

                async def publish(chunk: str) -> None:
                    from inspire.services.execution.async_output import deliver_output

                    if callback is not None:
                        await deliver_output(callback, chunk)
                    await queue.put(chunk)

                def on_output(chunk: str) -> Any:
                    bridge = async_call.get()
                    if bridge is not None:
                        return bridge(call(publish, chunk))
                    return publish(chunk)

                bound(*args, **dict(kwargs, on_output=on_output))
            else:
                iterator = bound(*args, **kwargs)
                try:
                    for value in iterator:
                        emit(value)
                finally:
                    close = getattr(iterator, "close", None)
                    if close is not None:
                        close()

        task = asyncio.create_task(self._invoke(facade, method, args, kwargs, produce))
        self._active.add(task)
        try:
            while True:
                read = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait((read, task), return_when=asyncio.FIRST_COMPLETED)
                    if read in done:
                        yield read.result()
                    else:
                        task.result()
                        break
                finally:
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
        finally:
            stopped.set()
            task.cancel()
            try:
                await _finish(asyncio.gather(task, return_exceptions=True))
            finally:
                self._active.discard(task)

    async def _shutdown(self) -> None:
        tasks = list(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self._client is not None:
                self._client.close()
        finally:
            try:
                await self._http_pool.aclose()
            finally:
                await self._offload_pool.close()

    async def close(self) -> None:
        self._check()
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._shutdown())
        await _finish(self._closing)


class AsyncFacade:
    def __init__(self, client: AsyncRuntime) -> None:
        self._client = client


async def _await_output(value: Any) -> Any:
    return await value
